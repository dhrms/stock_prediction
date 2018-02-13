"""
Project:    stock_prediction
File:       lstm.py
Created by: louise
On:         29/01/18
At:         4:56 PM
"""
import torch
from torch import nn
from torch.autograd import Variable
import torch.nn.functional as F

import numpy as np

from src.primaldual.linear_operators import GeneralLinearOperator, GeneralLinearAdjointOperator
from src.primaldual.primal_dual_updates import DualGeneralUpdate, PrimalGeneralUpdate, PrimalRegularization
from src.primaldual.proximal_operators import ProximalLinfBall, ProximalQuadraticForm


class LSTM(nn.Module):
    def __init__(self, hidden_size=64, hidden_size2=128, num_securities=5, dropout=0.2, n_layers=8, T=10, training=True):
        """

        :param hidden_size: int
        :param num_securities: int
        :param dropout: float
        :param training: bool
        """
        super(LSTM, self).__init__()
        self.training = training
        self.hidden_size = hidden_size
        self.hidden_size2 = hidden_size2
        self.rnn = nn.LSTM(
            input_size=num_securities,
            hidden_size=self.hidden_size,
            num_layers=n_layers,
            dropout=dropout,
            bidirectional=False
        )
        # self.rnn2 = nn.LSTM(
        #     input_size=num_securities,
        #     hidden_size=self.hidden_size2,
        #     num_layers=n_layers,
        #     dropout=dropout,
        #     bidirectional=False
        # )

        self.fc1 = nn.Linear(self.hidden_size, self.hidden_size)
        self.fc1.weight.data.normal_()
        self.fc3 = nn.Linear(self.hidden_size, 10)
        #self.bn1 = nn.BatchNorm1d(self.hidden_size)
        self.fc2 = nn.Linear(10, num_securities)
        self.relu = nn.ReLU()
        self.T = T

    def forward(self, x):
        """

        :param x: Pytorch Variable, T x batch_size x n_stocks
        :return:
        """
        batch_size = x.size()[1]
        seq_length = x.size()[0]

        x = x.view(seq_length, batch_size, -1)

        # We need to pass the initial cell states
        h0 = Variable(torch.zeros(self.rnn.num_layers, batch_size, self.hidden_size)).cuda()
        c0 = Variable(torch.zeros(self.rnn.num_layers, batch_size, self.hidden_size)).cuda()
        outputs, (ht, ct) = self.rnn(x, (h0, c0))
        # h1 = Variable(torch.zeros(self.rnn.num_layers, batch_size, self.hidden_size2)).cuda()
        # c1 = Variable(torch.zeros(self.rnn.num_layers, batch_size, self.hidden_size2)).cuda()
        # outputs, (ht, ct) = self.rnn2(x, (h1, c1))
        out = outputs[-1]  # We are only interested in the final prediction
        out = self.fc1(out)
        out = self.fc3(out)
        #out = self.bn1(out)
        out = self.relu(out)
        #out = F.dropout(out, training=self.training)
        out = self.fc2(out)
        return out


class PD_LSTM(nn.Module):
    def __init__(self, H, b, hidden_size=64, n_layers=2, num_securities=5, dropout=0.2,
                 max_it=20, sigma=0.5, tau=0.1, theta=0.9, lambda_rof=5.,
                 training=True):
        """

        :param hidden_size:
        :param num_securities:
        :param dropout:
        :param T:
        :param max_it:
        :param sigma:
        :param tau:
        :param theta:
        :param training:
        """
        super(PD_LSTM, self).__init__()
        self.training = training
        # LSTM
        self.hidden_size = hidden_size
        self.n_layers = n_layers
        self.rnn = nn.LSTM(
            input_size=num_securities,
            hidden_size=hidden_size,
            num_layers=n_layers,
            dropout=dropout,
            bidirectional=False, )
        self.fc1 = nn.Linear(self.hidden_size, self.hidden_size)
        self.fc1.weight.data.normal_()
        self.fc2 = nn.Linear(self.hidden_size, num_securities)

        self.linear_op = GeneralLinearOperator()
        self.linear_op_adj = GeneralLinearAdjointOperator()
        self.max_it = max_it
        self.prox_l_inf = ProximalLinfBall()
        self.prox_quad = ProximalQuadraticForm()
        self.primal_reg = PrimalRegularization(theta)

        self.pe = 0.0
        self.de = 0.0
        self.clambda = nn.Parameter(lambda_rof * torch.ones(1).type_as(H.data))
        self.sigma = nn.Parameter(sigma * torch.ones(1).type_as(H.data))
        self.tau = nn.Parameter(tau * torch.ones(1).type_as(H.data))
        self.theta = nn.Parameter(theta * torch.ones(1).type_as(H.data))
        self.primal_update = PrimalGeneralUpdate(self.tau)
        self.dual_update = DualGeneralUpdate(self.sigma)


        self.H = nn.Parameter(H.data)
        self.b = nn.Parameter(b.data)



    def forward(self, x, x_obs):
        """

        :param x: Pytorch Variable, T x batch_size x n_stocks, current estimated sequence
        :param x_obs: Pytorch Variable, T x batch_size x n_stocks, observed sequence
        :return:
        """
        batch_size = x.size()[1]
        seq_length = x.size()[0]
        n_stocks = x.size()[2]

        x = x.view(seq_length, batch_size, -1)

        # Encode time series through LSTM cells
        h0 = Variable(torch.zeros(self.n_layers, batch_size, self.hidden_size)).cuda()
        c0 = Variable(torch.zeros(self.n_layers, batch_size, self.hidden_size)).cuda()
        outputs, (ht, ct) = self.rnn(x, (h0, c0))  # seq_length x batch_size x n_stocks

        # Initialize variables for Primal Dual Net
        x_tilde = Variable(outputs.data.permute(1, 0, 2).clone()).type_as(x)  # batch_size x seq_length x hidden
        x = Variable(outputs.data.permute(1, 0, 2).clone()).type_as(x)  # batch_size x seq_length x hidden
        y = Variable(torch.ones((2, seq_length, self.hidden_size))).type_as(x)  # batch_size x 2 x seq_length x hidden
        x_y = Variable(torch.ones((2, seq_length, self.hidden_size))).type_as(x)  # batch_size x 2 x seq_length x hidden
        # Forward pass

        self.theta.data.clamp_(0, 5)
        for it in range(self.max_it):
            # Dual update
            Lx = self.linear_op.forward(x_tilde)  # Bx2xTxn_stocks
            y = self.dual_update.forward(x_tilde.unsqueeze(1), Lx)  # Bx2xTxn_stocks
            # y.data.clamp_(0, 1)
            y = self.prox_l_inf.forward(y, 1.0)  # Bx2xTxn_stocks
            # Primal update
            x_old = x
            Ladjy = self.linear_op_adj.forward(y)  # Bx1xTxn_stocks
            x = self.primal_update.forward(outputs, Ladjy)  # Bx1xTxn_stocks
            x = self.prox_quad.forward(x, self.H, self.b, self.tau)  # 1xTxn_stocks
            # x.data.clamp_(0, 1)
            # Smoothing
            x = x.view(-1, seq_length, self.hidden_size)
            x_tilde = self.primal_reg.forward(x, x_tilde, x_old)  # Bx1xTxn_stocks
            # x_tilde.data.clamp_(0, 1)

        out = self.fc1(x_tilde)
        out = self.fc2(out)
        return out[:, -1, :]
