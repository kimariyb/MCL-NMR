import torch
import torch.nn as nn

from typing import Union

from torch_geometric.nn import MLP
from torch_geometric.nn.conv import GINEConv as PyGGINEConv
from torch_geometric.nn.dense.linear import Linear as PyGLinear
from torch_geometric.typing import OptPairTensor, OptTensor, Size


class GINEConv(PyGGINEConv):
    def __init__(self, bond_encoder, in_channels, out_channels, **kwargs):
        mlp = MLP(
            channel_list=[in_channels, out_channels, out_channels],
            act="gelu",
            dropout=0.1,
        )
        super().__init__(nn=mlp, **kwargs)
        self.bond_encoder = nn.Sequential(
            bond_encoder, PyGLinear(-1, in_channels)
        )

    def forward(
        self,
        x: Union[torch.Tensor, OptPairTensor],
        edge_index: torch.Tensor,
        edge_attr: OptTensor = None,
        size: Size = None,
    ) -> torch.Tensor:

        if isinstance(x, torch.Tensor):
            x = (x, x)

        if self.bond_encoder is not None and edge_attr is not None:
            edge_attr = self.bond_encoder(edge_attr.float())

        # propagate_type: (x: OptPairTensor, edge_attr: OptTensor)
        out = self.propagate(edge_index, x=x, edge_attr=edge_attr, size=size)

        x_r = x[1]
        if x_r is not None:
            out = out + (1 + self.eps) * x_r

        return self.nn(out)

    def message(self, x_j: torch.Tensor, edge_attr: OptTensor) -> torch.Tensor:
        return x_j if edge_attr is None else x_j + edge_attr
