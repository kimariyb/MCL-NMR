"""
Copyright (c) Facebook, Inc. and its affiliates.
This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
"""

import ast
import collections
import copy
import importlib
import itertools
import json
import logging
import os
import sys
import time
import numpy as np
import torch
import torch.nn as nn
import torch_geometric
import yaml

from bisect import bisect
from functools import wraps
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, List, Mapping

from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.figure import Figure
from torch_geometric.data import Data
from torch_geometric.utils import remove_self_loops
from torch_scatter import segment_coo, segment_csr


def read_json(path):
    """ """
    if not path.endswith(".json"):
        raise UserWarning(f"Path {path} is not a json-path.")

    with open(path, "r") as f:
        content = json.load(f)
    return content


def update_json(path, data):
    """ """
    if not path.endswith(".json"):
        raise UserWarning(f"Path {path} is not a json-path.")

    content = read_json(path)
    content.update(data)
    write_json(path, content)


def write_json(path, data):
    """ """
    if not path.endswith(".json"):
        raise UserWarning(f"Path {path} is not a json-path.")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def read_value_json(path, key):
    """ """
    content = read_json(path)

    if key in content.keys():
        return content[key]
    else:
        return None


def repeat_blocks(
    sizes,
    repeats,
    continuous_indexing=True,
    start_idx=0,
    block_inc=0,
    repeat_inc=0,
):
    """Repeat blocks of indices.
    Adapted from https://stackoverflow.com/questions/51154989/numpy-vectorized-function-to-repeat-blocks-of-consecutive-elements
    continuous_indexing: Whether to keep increasing the index after each block
    start_idx: Starting index
    block_inc: Number to increment by after each block,
               either global or per block. Shape: len(sizes) - 1
    repeat_inc: Number to increment by after each repetition,
                either global or per block
    Examples
    --------
        sizes = [1,3,2] ; repeats = [3,2,3] ; continuous_indexing = False
        Return: [0 0 0  0 1 2 0 1 2  0 1 0 1 0 1]
        sizes = [1,3,2] ; repeats = [3,2,3] ; continuous_indexing = True
        Return: [0 0 0  1 2 3 1 2 3  4 5 4 5 4 5]
        sizes = [1,3,2] ; repeats = [3,2,3] ; continuous_indexing = True ;
        repeat_inc = 4
        Return: [0 4 8  1 2 3 5 6 7  4 5 8 9 12 13]
        sizes = [1,3,2] ; repeats = [3,2,3] ; continuous_indexing = True ;
        start_idx = 5
        Return: [5 5 5  6 7 8 6 7 8  9 10 9 10 9 10]
        sizes = [1,3,2] ; repeats = [3,2,3] ; continuous_indexing = True ;
        block_inc = 1
        Return: [0 0 0  2 3 4 2 3 4  6 7 6 7 6 7]
        sizes = [0,3,2] ; repeats = [3,2,3] ; continuous_indexing = True
        Return: [0 1 2 0 1 2  3 4 3 4 3 4]
        sizes = [2,3,2] ; repeats = [2,0,2] ; continuous_indexing = True
        Return: [0 1 0 1  5 6 5 6]
    """
    assert sizes.dim() == 1
    assert all(sizes >= 0)

    # Remove 0 sizes
    sizes_nonzero = sizes > 0
    if not torch.all(sizes_nonzero):
        assert block_inc == 0  # Implementing this is not worth the effort
        sizes = torch.masked_select(sizes, sizes_nonzero)
        if isinstance(repeats, torch.Tensor):
            repeats = torch.masked_select(repeats, sizes_nonzero)
        if isinstance(repeat_inc, torch.Tensor):
            repeat_inc = torch.masked_select(repeat_inc, sizes_nonzero)

    if isinstance(repeats, torch.Tensor):
        assert all(repeats >= 0)
        insert_dummy = repeats[0] == 0
        if insert_dummy:
            one = sizes.new_ones(1)
            zero = sizes.new_zeros(1)
            sizes = torch.cat((one, sizes))
            repeats = torch.cat((one, repeats))
            if isinstance(block_inc, torch.Tensor):
                block_inc = torch.cat((zero, block_inc))
            if isinstance(repeat_inc, torch.Tensor):
                repeat_inc = torch.cat((zero, repeat_inc))
    else:
        assert repeats >= 0
        insert_dummy = False

    # Get repeats for each group using group lengths/sizes
    r1 = torch.repeat_interleave(
        torch.arange(len(sizes), device=sizes.device), repeats
    )

    # Get total size of output array, as needed to initialize output indexing array
    N = (sizes * repeats).sum()

    # Initialize indexing array with ones as we need to setup incremental indexing
    # within each group when cumulatively summed at the final stage.
    # Two steps here:
    # 1. Within each group, we have multiple sequences, so setup the offsetting
    # at each sequence lengths by the seq. lengths preceding those.
    id_ar = torch.ones(N, dtype=torch.long, device=sizes.device)
    id_ar[0] = 0
    insert_index = sizes[r1[:-1]].cumsum(0)
    insert_val = (1 - sizes)[r1[:-1]]

    if isinstance(repeats, torch.Tensor) and torch.any(repeats == 0):
        diffs = r1[1:] - r1[:-1]
        indptr = torch.cat((sizes.new_zeros(1), diffs.cumsum(0)))
        if continuous_indexing:
            # If a group was skipped (repeats=0) we need to add its size
            insert_val += segment_csr(sizes[: r1[-1]], indptr, reduce="sum")

        # Add block increments
        if isinstance(block_inc, torch.Tensor):
            insert_val += segment_csr(
                block_inc[: r1[-1]], indptr, reduce="sum"
            )
        else:
            insert_val += block_inc * (indptr[1:] - indptr[:-1])
            if insert_dummy:
                insert_val[0] -= block_inc
    else:
        idx = r1[1:] != r1[:-1]
        if continuous_indexing:
            # 2. For each group, make sure the indexing starts from the next group's
            # first element. So, simply assign 1s there.
            insert_val[idx] = 1

        # Add block increments
        insert_val[idx] += block_inc

    # Add repeat_inc within each group
    if isinstance(repeat_inc, torch.Tensor):
        insert_val += repeat_inc[r1[:-1]]
        if isinstance(repeats, torch.Tensor):
            repeat_inc_inner = repeat_inc[repeats > 0][:-1]
        else:
            repeat_inc_inner = repeat_inc[:-1]
    else:
        insert_val += repeat_inc
        repeat_inc_inner = repeat_inc

    # Subtract the increments between groups
    if isinstance(repeats, torch.Tensor):
        repeats_inner = repeats[repeats > 0][:-1]
    else:
        repeats_inner = repeats
    insert_val[r1[1:] != r1[:-1]] -= repeat_inc_inner * repeats_inner

    # Assign index-offsetting values
    id_ar[insert_index] = insert_val

    if insert_dummy:
        id_ar = id_ar[1:]
        if continuous_indexing:
            id_ar[0] -= 1

    # Set start index now, in case of insertion due to leading repeats=0
    id_ar[0] += start_idx

    # Finally index into input array for the group repeated o/p
    res = id_ar.cumsum(0)
    return res


def get_edge_id(edge_idx, cell_offsets, num_atoms):
    cell_basis = cell_offsets.max() - cell_offsets.min() + 1
    cell_id = (
        (
            cell_offsets
            * cell_offsets.new_tensor([[1, cell_basis, cell_basis**2]])
        )
        .sum(-1)
        .long()
    )
    edge_id = edge_idx[0] + edge_idx[1] * num_atoms + cell_id * num_atoms**2
    return edge_id

if TYPE_CHECKING:
    from torch.nn.modules.module import _IncompatibleKeys


def pyg2_data_transform(data: Data):
    """
    if we're on the new pyg (2.0 or later) and if the Data stored is in older format
    we need to convert the data to the new format
    """
    if torch_geometric.__version__ >= "2.0" and "_store" not in data.__dict__:
        return Data(
            **{k: v for k, v in data.__dict__.items() if v is not None}
        )

    return data


def save_checkpoint(
    state, checkpoint_dir="checkpoints/", checkpoint_file="checkpoint.pt"
):
    filename = os.path.join(checkpoint_dir, checkpoint_file)
    torch.save(state, filename)
    return filename


class Complete(object):
    def __call__(self, data):
        device = data.edge_index.device

        row = torch.arange(data.num_nodes, dtype=torch.long, device=device)
        col = torch.arange(data.num_nodes, dtype=torch.long, device=device)

        row = row.view(-1, 1).repeat(1, data.num_nodes).view(-1)
        col = col.repeat(data.num_nodes)
        edge_index = torch.stack([row, col], dim=0)

        edge_attr = None
        if data.edge_attr is not None:
            idx = data.edge_index[0] * data.num_nodes + data.edge_index[1]
            size = list(data.edge_attr.size())
            size[0] = data.num_nodes * data.num_nodes
            edge_attr = data.edge_attr.new_zeros(size)
            edge_attr[idx] = data.edge_attr

        edge_index, edge_attr = remove_self_loops(edge_index, edge_attr)
        data.edge_attr = edge_attr
        data.edge_index = edge_index

        return data


def warmup_lr_lambda(current_step, optim_config):
    """Returns a learning rate multiplier.
    Till `warmup_steps`, learning rate linearly increases to `initial_lr`,
    and then gets multiplied by `lr_gamma` every time a milestone is crossed.
    """

    # keep this block for older configs that have warmup_epochs instead of warmup_steps
    # and lr_milestones are defined in epochs
    if (
        any(x < 100 for x in optim_config["lr_milestones"])
        or "warmup_epochs" in optim_config
    ):
        raise Exception(
            "ConfigError: please define lr_milestones in steps not epochs and define warmup_steps instead of warmup_epochs"
        )

    if current_step <= optim_config["warmup_steps"]:
        alpha = current_step / float(optim_config["warmup_steps"])
        return optim_config["warmup_factor"] * (1.0 - alpha) + alpha
    else:
        idx = bisect(optim_config["lr_milestones"], current_step)
        return pow(optim_config["lr_gamma"], idx)


def print_cuda_usage():
    print("Memory Allocated:", torch.cuda.memory_allocated() / (1024 * 1024))
    print(
        "Max Memory Allocated:",
        torch.cuda.max_memory_allocated() / (1024 * 1024),
    )
    print("Memory Cached:", torch.cuda.memory_cached() / (1024 * 1024))
    print("Max Memory Cached:", torch.cuda.max_memory_cached() / (1024 * 1024))


def conditional_grad(dec):
    "Decorator to enable/disable grad depending on whether force/energy predictions are being made"
    # Adapted from https://stackoverflow.com/questions/60907323/accessing-class-property-as-decorator-argument
    def decorator(func):
        @wraps(func)
        def cls_method(self, *args, **kwargs):
            f = func
            # if self.regress_forces and not getattr(self, "direct_forces", 0):
            #     f = dec(func)
            return f(self, *args, **kwargs)

        return cls_method

    return decorator


def plot_histogram(data, xlabel="", ylabel="", title=""):
    assert isinstance(data, list)

    # Preset
    fig = Figure(figsize=(5, 4), dpi=150)
    canvas = FigureCanvas(fig)
    ax = fig.gca()

    # Plot
    ax.hist(data, bins=20, rwidth=0.9, zorder=3)

    # Axes
    ax.grid(color="0.95", zorder=0)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    fig.tight_layout(pad=2)

    # Return numpy array
    canvas.draw()
    image_from_plot = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
    image_from_plot = image_from_plot.reshape(
        fig.canvas.get_width_height()[::-1] + (3,)
    )

    return image_from_plot


# Override the collation method in `pytorch_geometric.data.InMemoryDataset`
def collate(data_list):
    keys = data_list[0].keys
    data = data_list[0].__class__()

    for key in keys:
        data[key] = []
    slices = {key: [0] for key in keys}

    for item, key in product(data_list, keys):
        data[key].append(item[key])
        if torch.is_tensor(item[key]):
            s = slices[key][-1] + item[key].size(
                item.__cat_dim__(key, item[key])
            )
        elif isinstance(item[key], int) or isinstance(item[key], float):
            s = slices[key][-1] + 1
        else:
            raise ValueError("Unsupported attribute type")
        slices[key].append(s)

    if hasattr(data_list[0], "__num_nodes__"):
        data.__num_nodes__ = []
        for item in data_list:
            data.__num_nodes__.append(item.num_nodes)

    for key in keys:
        if torch.is_tensor(data_list[0][key]):
            data[key] = torch.cat(
                data[key], dim=data.__cat_dim__(key, data_list[0][key])
            )
        else:
            data[key] = torch.tensor(data[key])
        slices[key] = torch.tensor(slices[key], dtype=torch.long)

    return data, slices


def add_edge_distance_to_graph(
    batch,
    device="cpu",
    dmin=0.0,
    dmax=6.0,
    num_gaussians=50,
):
    # Make sure x has positions.
    if not all(batch.pos[0][:] == batch.x[0][-3:]):
        batch.x = torch.cat([batch.x, batch.pos.float()], dim=1)
    # First set computations to be tracked for positions.
    batch.x = batch.x.requires_grad_(True)
    # Then compute Euclidean distance between edge endpoints.
    pdist = torch.nn.PairwiseDistance(p=2.0)
    distances = pdist(
        batch.x[batch.edge_index[0]][:, -3:],
        batch.x[batch.edge_index[1]][:, -3:],
    )
    # Expand it using a gaussian basis filter.
    gdf_filter = torch.linspace(dmin, dmax, num_gaussians)
    var = gdf_filter[1] - gdf_filter[0]
    gdf_filter, var = gdf_filter.to(device), var.to(device)
    gdf_distances = torch.exp(
        -((distances.view(-1, 1) - gdf_filter) ** 2) / var**2
    )
    # Reassign edge attributes.
    batch.edge_weight = distances
    batch.edge_attr = gdf_distances.float()
    return batch


def _import_local_file(path: Path, *, project_root: Path):
    """
    Imports a Python file as a module
    :param path: The path to the file to import
    :type path: Path
    :param project_root: The root directory of the project (i.e., the "ocp" folder)
    :type project_root: Path
    """

    path = path.resolve()
    project_root = project_root.resolve()

    module_name = ".".join(
        path.absolute()
        .relative_to(project_root.absolute())
        .with_suffix("")
        .parts
    )
    logging.debug(f"Resolved module name of {path} to {module_name}")
    importlib.import_module(module_name)


def setup_experimental_imports(project_root: Path):
    experimental_folder = (project_root / "experimental").resolve()
    if not experimental_folder.exists() or not experimental_folder.is_dir():
        return

    experimental_files = [
        f.resolve().absolute() for f in experimental_folder.rglob("*.py")
    ]
    # Ignore certain directories within experimental
    ignore_file = experimental_folder / ".ignore"
    if ignore_file.exists():
        with open(ignore_file, "r") as f:
            for line in f.read().splitlines():
                for ignored_file in (experimental_folder / line).rglob("*.py"):
                    experimental_files.remove(
                        ignored_file.resolve().absolute()
                    )

    for f in experimental_files:
        _import_local_file(f, project_root=project_root)


def dict_set_recursively(dictionary, key_sequence, val):
    top_key = key_sequence.pop(0)
    if len(key_sequence) == 0:
        dictionary[top_key] = val
    else:
        if top_key not in dictionary:
            dictionary[top_key] = {}
        dict_set_recursively(dictionary[top_key], key_sequence, val)


def parse_value(value):
    """
    Parse string as Python literal if possible and fallback to string.
    """
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        # Use as string if nothing else worked
        return value


def create_dict_from_args(args: list, sep: str = "."):
    """
    Create a (nested) dictionary from console arguments.
    Keys in different dictionary levels are separated by sep.
    """
    return_dict = {}
    for arg in args:
        arg = arg.strip("--")
        keys_concat, val = arg.split("=")
        val = parse_value(val)
        key_sequence = keys_concat.split(sep)
        dict_set_recursively(return_dict, key_sequence, val)
    return return_dict


def load_config(path: str, previous_includes: list = []):
    path = Path(path)
    if path in previous_includes:
        raise ValueError(
            f"Cyclic config include detected. {path} included in sequence {previous_includes}."
        )
    previous_includes = previous_includes + [path]

    direct_config = yaml.safe_load(open(path, "r"))

    # Load config from included files.
    if "includes" in direct_config:
        includes = direct_config.pop("includes")
    else:
        includes = []
    if not isinstance(includes, list):
        raise AttributeError(
            "Includes must be a list, '{}' provided".format(type(includes))
        )

    config = {}
    duplicates_warning = []
    duplicates_error = []

    for include in includes:
        include_config, inc_dup_warning, inc_dup_error = load_config(
            include, previous_includes
        )
        duplicates_warning += inc_dup_warning
        duplicates_error += inc_dup_error

        # Duplicates between includes causes an error
        config, merge_dup_error = merge_dicts(config, include_config)
        duplicates_error += merge_dup_error

    # Duplicates between included and main file causes warnings
    config, merge_dup_warning = merge_dicts(config, direct_config)
    duplicates_warning += merge_dup_warning

    return config, duplicates_warning, duplicates_error


def build_config(args, args_override):
    config, duplicates_warning, duplicates_error = load_config(args.config_yml)
    if len(duplicates_warning) > 0:
        logging.warning(
            f"Overwritten config parameters from included configs "
            f"(non-included parameters take precedence): {duplicates_warning}"
        )
    if len(duplicates_error) > 0:
        raise ValueError(
            f"Conflicting (duplicate) parameters in simultaneously "
            f"included configs: {duplicates_error}"
        )

    # Check for overridden parameters.
    if args_override != []:
        overrides = create_dict_from_args(args_override)
        config, _ = merge_dicts(config, overrides)

    # Some other flags.
    config["mode"] = args.mode
    config["identifier"] = args.identifier
    config["timestamp_id"] = args.timestamp_id
    config["seed"] = args.seed
    config["is_debug"] = args.debug
    config["run_dir"] = args.run_dir
    config["print_every"] = args.print_every
    config["amp"] = args.amp
    config["checkpoint"] = args.checkpoint
    config["cpu"] = args.cpu
    # Submit
    config["submit"] = args.submit
    config["summit"] = args.summit
    # Distributed
    config["local_rank"] = args.local_rank
    config["distributed_port"] = args.distributed_port
    config["world_size"] = args.num_nodes * args.num_gpus
    config["distributed_backend"] = args.distributed_backend
    config["noddp"] = args.no_ddp
    config["gp_gpus"] = args.gp_gpus

    return config


def create_grid(base_config, sweep_file):
    def _flatten_sweeps(sweeps, root_key="", sep="."):
        flat_sweeps = []
        for key, value in sweeps.items():
            new_key = root_key + sep + key if root_key else key
            if isinstance(value, collections.MutableMapping):
                flat_sweeps.extend(_flatten_sweeps(value, new_key).items())
            else:
                flat_sweeps.append((new_key, value))
        return collections.OrderedDict(flat_sweeps)

    def _update_config(config, keys, override_vals, sep="."):
        for key, value in zip(keys, override_vals):
            key_path = key.split(sep)
            child_config = config
            for name in key_path[:-1]:
                child_config = child_config[name]
            child_config[key_path[-1]] = value
        return config

    sweeps = yaml.safe_load(open(sweep_file, "r"))
    flat_sweeps = _flatten_sweeps(sweeps)
    keys = list(flat_sweeps.keys())
    values = list(itertools.product(*flat_sweeps.values()))

    configs = []
    for i, override_vals in enumerate(values):
        config = copy.deepcopy(base_config)
        config = _update_config(config, keys, override_vals)
        config["identifier"] = config["identifier"] + f"_run{i}"
        configs.append(config)
    return configs


def save_experiment_log(args, jobs, configs):
    log_file = args.logdir / "exp" / time.strftime("%Y-%m-%d-%I-%M-%S%p.log")
    log_file.parent.mkdir(exist_ok=True, parents=True)
    with open(log_file, "w") as f:
        for job, config in zip(jobs, configs):
            print(
                json.dumps(
                    {
                        "config": config,
                        "slurm_id": job.job_id,
                        "timestamp": time.strftime("%I:%M:%S%p %Z %b %d, %Y"),
                    }
                ),
                file=f,
            )
    return log_file


def get_max_neighbors_mask(
    natoms, index, atom_distance, max_num_neighbors_threshold
):
    """
    Give a mask that filters out edges so that each atom has at most
    `max_num_neighbors_threshold` neighbors.
    Assumes that `index` is sorted.
    """
    device = natoms.device
    num_atoms = natoms.sum()

    # Get number of neighbors
    # segment_coo assumes sorted index
    ones = index.new_ones(1).expand_as(index)
    num_neighbors = segment_coo(ones, index, dim_size=num_atoms)
    max_num_neighbors = num_neighbors.max()
    num_neighbors_thresholded = num_neighbors.clamp(
        max=max_num_neighbors_threshold
    )

    # Get number of (thresholded) neighbors per image
    image_indptr = torch.zeros(
        natoms.shape[0] + 1, device=device, dtype=torch.long
    )
    image_indptr[1:] = torch.cumsum(natoms, dim=0)
    num_neighbors_image = segment_csr(num_neighbors_thresholded, image_indptr)

    # If max_num_neighbors is below the threshold, return early
    if (
        max_num_neighbors <= max_num_neighbors_threshold
        or max_num_neighbors_threshold <= 0
    ):
        mask_num_neighbors = torch.tensor(
            [True], dtype=bool, device=device
        ).expand_as(index)
        return mask_num_neighbors, num_neighbors_image

    # Create a tensor of size [num_atoms, max_num_neighbors] to sort the distances of the neighbors.
    # Fill with infinity so we can easily remove unused distances later.
    distance_sort = torch.full(
        [num_atoms * max_num_neighbors], np.inf, device=device
    )

    # Create an index map to map distances from atom_distance to distance_sort
    # index_sort_map assumes index to be sorted
    index_neighbor_offset = torch.cumsum(num_neighbors, dim=0) - num_neighbors
    index_neighbor_offset_expand = torch.repeat_interleave(
        index_neighbor_offset, num_neighbors
    )
    index_sort_map = (
        index * max_num_neighbors
        + torch.arange(len(index), device=device)
        - index_neighbor_offset_expand
    )
    distance_sort.index_copy_(0, index_sort_map, atom_distance)
    distance_sort = distance_sort.view(num_atoms, max_num_neighbors)

    # Sort neighboring atoms based on distance
    distance_sort, index_sort = torch.sort(distance_sort, dim=1)
    # Select the max_num_neighbors_threshold neighbors that are closest
    distance_sort = distance_sort[:, :max_num_neighbors_threshold]
    index_sort = index_sort[:, :max_num_neighbors_threshold]

    # Offset index_sort so that it indexes into index
    index_sort = index_sort + index_neighbor_offset.view(-1, 1).expand(
        -1, max_num_neighbors_threshold
    )
    # Remove "unused pairs" with infinite distances
    mask_finite = torch.isfinite(distance_sort)
    index_sort = torch.masked_select(index_sort, mask_finite)

    # At this point index_sort contains the index into index of the
    # closest max_num_neighbors_threshold neighbors per atom
    # Create a mask to remove all pairs not in index_sort
    mask_num_neighbors = torch.zeros(len(index), device=device, dtype=bool)
    mask_num_neighbors.index_fill_(0, index_sort, True)

    return mask_num_neighbors, num_neighbors_image


def get_pruned_edge_idx(edge_index, num_atoms=None, max_neigh=1e9):
    assert num_atoms is not None

    # removes neighbors > max_neigh
    # assumes neighbors are sorted in increasing distance
    _nonmax_idx = []
    for i in range(num_atoms):
        idx_i = torch.arange(len(edge_index[1]))[(edge_index[1] == i)][
            :max_neigh
        ]
        _nonmax_idx.append(idx_i)
    _nonmax_idx = torch.cat(_nonmax_idx)

    return _nonmax_idx


def merge_dicts(dict1: dict, dict2: dict):
    """Recursively merge two dictionaries.
    Values in dict2 override values in dict1. If dict1 and dict2 contain a dictionary as a
    value, this will call itself recursively to merge these dictionaries.
    This does not modify the input dictionaries (creates an internal copy).
    Additionally returns a list of detected duplicates.
    Adapted from https://github.com/TUM-DAML/seml/blob/master/seml/utils.py
    Parameters
    ----------
    dict1: dict
        First dict.
    dict2: dict
        Second dict. Values in dict2 will override values from dict1 in case they share the same key.
    Returns
    -------
    return_dict: dict
        Merged dictionaries.
    """
    if not isinstance(dict1, dict):
        raise ValueError(f"Expecting dict1 to be dict, found {type(dict1)}.")
    if not isinstance(dict2, dict):
        raise ValueError(f"Expecting dict2 to be dict, found {type(dict2)}.")

    return_dict = copy.deepcopy(dict1)
    duplicates = []

    for k, v in dict2.items():
        if k not in dict1:
            return_dict[k] = v
        else:
            if isinstance(v, dict) and isinstance(dict1[k], dict):
                return_dict[k], duplicates_k = merge_dicts(dict1[k], dict2[k])
                duplicates += [f"{k}.{dup}" for dup in duplicates_k]
            else:
                return_dict[k] = dict2[k]
                duplicates.append(k)

    return return_dict, duplicates


class SeverityLevelBetween(logging.Filter):
    def __init__(self, min_level, max_level):
        super().__init__()
        self.min_level = min_level
        self.max_level = max_level

    def filter(self, record):
        return self.min_level <= record.levelno < self.max_level


def setup_logging():
    root = logging.getLogger()

    # Perform setup only if logging has not been configured
    if not root.hasHandlers():
        root.setLevel(logging.INFO)

        log_formatter = logging.Formatter(
            "%(asctime)s (%(levelname)s): %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Send INFO to stdout
        handler_out = logging.StreamHandler(sys.stdout)
        handler_out.addFilter(
            SeverityLevelBetween(logging.INFO, logging.WARNING)
        )
        handler_out.setFormatter(log_formatter)
        root.addHandler(handler_out)

        # Send WARNING (and higher) to stderr
        handler_err = logging.StreamHandler(sys.stderr)
        handler_err.setLevel(logging.WARNING)
        handler_err.setFormatter(log_formatter)
        root.addHandler(handler_err)


def compute_neighbors(pos, natoms, edge_index):
    # Get number of neighbors
    # segment_coo assumes sorted index
    ones = edge_index[1].new_ones(1).expand_as(edge_index[1])
    num_neighbors = segment_coo(
        ones, edge_index[1], dim_size=natoms.sum()
    )

    # Get number of neighbors per image
    image_indptr = torch.zeros(
        natoms.shape[0] + 1, device=pos.device, dtype=torch.long
    )
    image_indptr[1:] = torch.cumsum(natoms, dim=0)
    neighbors = segment_csr(num_neighbors, image_indptr)
    return neighbors


def check_traj_files(batch, traj_dir):
    if traj_dir is None:
        return False
    traj_dir = Path(traj_dir)
    traj_files = [traj_dir / f"{id}.traj" for id in batch[0].sid.tolist()]
    return all(fl.exists() for fl in traj_files)


def _resolve_scale_factor_submodule(model: nn.Module, name: str):
    from models.models_3d.layers import ScaleFactor

    try:
        scale = model.get_submodule(name)
        if not isinstance(scale, ScaleFactor):
            return None
        return scale
    except AttributeError:
        return None


def _report_incompat_keys(
    model: nn.Module,
    keys: "_IncompatibleKeys",
    strict: bool = False,
):
    # filter out the missing scale factor keys for the new scaling factor module
    missing_keys: List[str] = []
    for full_key_name in keys.missing_keys:
        parent_module_name, _ = full_key_name.rsplit(".", 1)
        scale_factor = _resolve_scale_factor_submodule(
            model, parent_module_name
        )
        if scale_factor is not None:
            continue
        missing_keys.append(full_key_name)

    # filter out unexpected scale factor keys that remain from the old scaling modules
    unexpected_keys: List[str] = []
    for full_key_name in keys.unexpected_keys:
        parent_module_name, _ = full_key_name.rsplit(".", 1)
        scale_factor = _resolve_scale_factor_submodule(
            model, parent_module_name
        )
        if scale_factor is not None:
            continue
        unexpected_keys.append(full_key_name)

    error_msgs = []
    if len(unexpected_keys) > 0:
        error_msgs.insert(
            0,
            "Unexpected key(s) in state_dict: {}. ".format(
                ", ".join('"{}"'.format(k) for k in unexpected_keys)
            ),
        )
    if len(missing_keys) > 0:
        error_msgs.insert(
            0,
            "Missing key(s) in state_dict: {}. ".format(
                ", ".join('"{}"'.format(k) for k in missing_keys)
            ),
        )

    if len(error_msgs) > 0:
        error_msg = "Error(s) in loading state_dict for {}:\n\t{}".format(
            model.__class__.__name__, "\n\t".join(error_msgs)
        )
        if strict:
            raise RuntimeError(error_msg)
        else:
            logging.warning(error_msg)

    return missing_keys, unexpected_keys


def load_state_dict(
    module: nn.Module,
    state_dict: Mapping[str, torch.Tensor],
    strict: bool = True,
):
    incompat_keys = module.load_state_dict(state_dict, strict=False)  # type: ignore
    return _report_incompat_keys(module, incompat_keys, strict=strict)

def get_natoms(batch):
    _, natoms = torch.unique(batch, return_counts=True)
    return natoms

def inner_product_normalized(x, y):
    """
    Calculate the inner product between the given normalized vectors,
    giving a result between -1 and 1.
    """
    return torch.sum(x * y, dim=-1).clamp(min=-1, max=1)


def mask_neighbors(neighbors, edge_mask):
    neighbors_old_indptr = torch.cat([neighbors.new_zeros(1), neighbors])
    neighbors_old_indptr = torch.cumsum(neighbors_old_indptr, dim=0)
    neighbors = segment_csr(edge_mask.long(), neighbors_old_indptr)
    return neighbors   

def ragged_range(sizes):
    """Multiple concatenated ranges.
    Examples
    --------
        sizes = [1 4 2 3]
        Return: [0  0 1 2 3  0 1  0 1 2]
    """
    assert sizes.dim() == 1
    if sizes.sum() == 0:
        return sizes.new_empty(0)

    # Remove 0 sizes
    sizes_nonzero = sizes > 0
    if not torch.all(sizes_nonzero):
        sizes = torch.masked_select(sizes, sizes_nonzero)

    # Initialize indexing array with ones as we need to setup incremental indexing
    # within each group when cumulatively summed at the final stage.
    id_steps = torch.ones(sizes.sum(), dtype=torch.long, device=sizes.device)
    id_steps[0] = 0
    insert_index = sizes[:-1].cumsum(0)
    insert_val = (1 - sizes)[:-1]

    # Assign index-offsetting values
    id_steps[insert_index] = insert_val

    # Finally index into input array for the group repeated o/p
    res = id_steps.cumsum(0)
    return res