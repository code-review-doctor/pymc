#   Copyright 2021 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
import functools
import warnings

from typing import Callable, Dict, List, Optional, Sequence, Set, Union

import aesara
import aesara.tensor as at
import numpy as np

from aesara.graph.basic import Variable, graph_inputs
from aesara.graph.fg import FunctionGraph
from aesara.tensor.var import TensorVariable

from pymc.aesaraf import compile_pymc
from pymc.util import get_transformed_name, get_untransformed_name, is_transformed_name

StartDict = Dict[Union[Variable, str], Union[np.ndarray, Variable, str]]
PointType = Dict[str, np.ndarray]


def convert_str_to_rv_dict(
    model, start: StartDict
) -> Dict[TensorVariable, Optional[Union[np.ndarray, Variable, str]]]:
    """Helper function for converting a user-provided start dict with str keys of (transformed) variable names
    to a dict mapping the RV tensors to untransformed initvals.
    TODO: Deprecate this functionality and only accept TensorVariables as keys
    """
    initvals = {}
    for key, initval in start.items():
        if isinstance(key, str):
            if is_transformed_name(key):
                rv = model[get_untransformed_name(key)]
                initvals[rv] = model.rvs_to_values[rv].tag.transform.backward(
                    initval, *rv.owner.inputs
                )
            else:
                initvals[model[key]] = initval
        else:
            initvals[key] = initval
    return initvals


def filter_rvs_to_jitter(step) -> Set[TensorVariable]:
    """Find the set of RVs for which the responsible step methods ask for
    the addition of jitter to the initial point.

    Parameters
    ----------
    step : BlockedStep or CompoundStep
        One or many step methods that were assigned model variables.

    Returns
    -------
    rvs_to_jitter : set
        The random variables for which jitter should be added.
    """
    # TODO: implement this
    return set()


def make_initial_point_fns_per_chain(
    *,
    model,
    overrides: Optional[Union[StartDict, Sequence[Optional[StartDict]]]],
    jitter_rvs: Set[TensorVariable],
    chains: int,
) -> List[Callable]:
    """Create an initial point function for each chain, as defined by initvals

    If a single initval dictionary is passed, the function is replicated for each
    chain, otherwise a unique function is compiled for each entry in the dictionary.

    Parameters
    ----------
    overrides : optional, list or dict
        Initial value strategy overrides that should take precedence over the defaults from the model.
        A sequence of None or dicts will be treated as chain-wise strategies and must have the same length as `seeds`.
    jitter_rvs : set
        Random variable tensors for which U(-1, 1) jitter shall be applied.
        (To the transformed space if applicable.)

    Raises
    ------
    ValueError
        If the number of entries in initvals is different than the number of chains

    """
    if isinstance(overrides, dict) or overrides is None:
        # One strategy for all chains
        # Only one function compilation is needed.
        ipfns = [
            make_initial_point_fn(
                model=model,
                overrides=overrides,
                jitter_rvs=jitter_rvs,
                return_transformed=True,
            )
        ] * chains
    elif len(overrides) == chains:
        ipfns = [
            make_initial_point_fn(
                model=model,
                jitter_rvs=jitter_rvs,
                overrides=chain_overrides,
                return_transformed=True,
            )
            for chain_overrides in overrides
        ]
    else:
        raise ValueError(
            f"Number of initval dicts ({len(overrides)}) does not match the number of chains ({chains})."
        )

    return ipfns


def make_initial_point_fn(
    *,
    model,
    overrides: Optional[StartDict] = None,
    jitter_rvs: Optional[Set[TensorVariable]] = None,
    default_strategy: str = "moment",
    return_transformed: bool = True,
) -> Callable:
    """Create seeded function that computes initial values for all free model variables.

    Parameters
    ----------
    jitter_rvs : set
        The set (or list or tuple) of random variables for which a U(-1, +1) jitter should be
        added to the initial value. Only available for variables that have a transform or real-valued support.
    default_strategy : str
        Which of { "moment", "prior" } to prefer if the initval setting for an RV is None.
    overrides : dict
        Initial value (strategies) to use instead of what's specified in `Model.initial_values`.
    return_transformed : bool
        If `True` the returned variables will correspond to transformed initial values.
    """

    def find_rng_nodes(variables):
        return [
            node
            for node in graph_inputs(variables)
            if isinstance(
                node,
                (
                    at.random.var.RandomStateSharedVariable,
                    at.random.var.RandomGeneratorSharedVariable,
                ),
            )
        ]

    sdict_overrides = convert_str_to_rv_dict(model, overrides or {})
    initval_strats = {
        **model.initial_values,
        **sdict_overrides,
    }

    initial_values = make_initial_point_expression(
        free_rvs=model.free_RVs,
        rvs_to_values=model.rvs_to_values,
        initval_strategies=initval_strats,
        jitter_rvs=jitter_rvs,
        default_strategy=default_strategy,
        return_transformed=return_transformed,
    )

    # Replace original rng shared variables so that we don't mess with them
    # when calling the final seeded function
    graph = FunctionGraph(outputs=initial_values, clone=False)
    rng_nodes = find_rng_nodes(graph.outputs)
    new_rng_nodes: List[Union[np.random.RandomState, np.random.Generator]] = []
    for rng_node in rng_nodes:
        rng_cls: type
        if isinstance(rng_node, at.random.var.RandomStateSharedVariable):
            rng_cls = np.random.RandomState
        else:
            rng_cls = np.random.Generator
        new_rng_nodes.append(aesara.shared(rng_cls(np.random.PCG64())))
    graph.replace_all(zip(rng_nodes, new_rng_nodes), import_missing=True)
    func = compile_pymc(inputs=[], outputs=graph.outputs, mode=aesara.compile.mode.FAST_COMPILE)

    varnames = []
    for var in model.free_RVs:
        transform = getattr(model.rvs_to_values[var].tag, "transform", None)
        if transform is not None and return_transformed:
            name = get_transformed_name(var.name, transform)
        else:
            name = var.name
        varnames.append(name)

    def make_seeded_function(func):

        rngs = find_rng_nodes(func.maker.fgraph.outputs)

        @functools.wraps(func)
        def inner(seed, *args, **kwargs):
            seeds = [
                np.random.PCG64(sub_seed)
                for sub_seed in np.random.SeedSequence(seed).spawn(len(rngs))
            ]
            for rng, seed in zip(rngs, seeds):
                if isinstance(rng, at.random.var.RandomStateSharedVariable):
                    new_rng = np.random.RandomState(seed)
                else:
                    new_rng = np.random.Generator(seed)
                rng.set_value(new_rng, True)
            values = func(*args, **kwargs)
            return dict(zip(varnames, values))

        return inner

    return make_seeded_function(func)


def make_initial_point_expression(
    *,
    free_rvs: Sequence[TensorVariable],
    rvs_to_values: Dict[TensorVariable, TensorVariable],
    initval_strategies: Dict[TensorVariable, Optional[Union[np.ndarray, Variable, str]]],
    jitter_rvs: Set[TensorVariable] = None,
    default_strategy: str = "moment",
    return_transformed: bool = False,
) -> List[TensorVariable]:
    """Creates the tensor variables that need to be evaluated to obtain an initial point.

    Parameters
    ----------
    free_rvs : list
        Tensors of free random variables in the model.
    rvs_to_values : dict
        Mapping of free random variable tensors to value variable tensors.
    initval_strategies : dict
        Mapping of free random variable tensors to initial value strategies.
        For example the `Model.initial_values` dictionary.
    jitter_rvs : set
        The set (or list or tuple) of random variables for which a U(-1, +1) jitter should be
        added to the initial value. Only available for variables that have a transform or real-valued support.
    default_strategy : str
        Which of { "moment", "prior" } to prefer if the initval strategy setting for an RV is None.
    return_transformed : bool
        Switches between returning the tensors for untransformed or transformed initial points.

    Returns
    -------
    initial_points : list of TensorVariable
        Aesara expressions for initial values of the free random variables.
    """
    from pymc.distributions.distribution import moment

    if jitter_rvs is None:
        jitter_rvs = set()

    initial_values = []
    initial_values_transformed = []

    for variable in free_rvs:
        strategy = initval_strategies.get(variable, None)

        if strategy is None:
            strategy = default_strategy

        if isinstance(strategy, str):
            if strategy == "moment":
                try:
                    value = moment(variable)
                except NotImplementedError:
                    warnings.warn(
                        f"Moment not defined for variable {variable} of type "
                        f"{variable.owner.op.__class__.__name__}, defaulting to "
                        f"a draw from the prior. This can lead to difficulties "
                        f"during tuning. You can manually define an initval or "
                        f"implement a moment dispatched function for this "
                        f"distribution.",
                        UserWarning,
                    )
                    value = variable
            elif strategy == "prior":
                value = variable
            else:
                raise ValueError(
                    f'Invalid string strategy: {strategy}. It must be one of ["moment", "prior"]'
                )
        else:
            value = at.as_tensor(strategy, dtype=variable.dtype).astype(variable.dtype)

        transform = getattr(rvs_to_values[variable].tag, "transform", None)

        if transform is not None:
            value = transform.forward(value, *variable.owner.inputs)

        if variable in jitter_rvs:
            jitter = at.random.uniform(-1, 1, size=value.shape)
            jitter.name = f"{variable.name}_jitter"
            value = value + jitter

        value = value.astype(variable.dtype)
        initial_values_transformed.append(value)

        if transform is not None:
            value = transform.backward(value, *variable.owner.inputs)

        initial_values.append(value)

    all_outputs: List[TensorVariable] = []
    all_outputs.extend(free_rvs)
    all_outputs.extend(initial_values)
    all_outputs.extend(initial_values_transformed)

    copy_graph = FunctionGraph(outputs=all_outputs, clone=True)

    n_variables = len(free_rvs)
    free_rvs_clone = copy_graph.outputs[:n_variables]
    initial_values_clone = copy_graph.outputs[n_variables:-n_variables]
    initial_values_transformed_clone = copy_graph.outputs[-n_variables:]

    # We now replace all rvs by the respective initial_point expressions
    # in the constrained (untransformed) space. We do this in reverse topological
    # order, so that later nodes do not reintroduce expressions with earlier
    # rvs that would need to once again be replaced by their initial_points
    graph = FunctionGraph(outputs=free_rvs_clone, clone=False)
    replacements = reversed(list(zip(free_rvs_clone, initial_values_clone)))
    graph.replace_all(replacements, import_missing=True)

    if not return_transformed:
        return graph.outputs
    # Because the unconstrained (transformed) expressions are a subgraph of the
    # constrained initial point they were also automatically updated inplace
    # when calling graph.replace_all above, so we don't need to do anything else
    return initial_values_transformed_clone
