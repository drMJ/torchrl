# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from __future__ import annotations

import abc
import contextlib
import functools

import importlib.util
import inspect
import os
import re
import warnings
from enum import Enum
from typing import Any, Dict, List, Union

import torch

from tensordict import (
    is_tensor_collection,
    LazyStackedTensorDict,
    TensorDict,
    TensorDictBase,
    unravel_key,
)
from tensordict.nn import TensorDictModule, TensorDictModuleBase
from tensordict.nn.probabilistic import (  # noqa
    # Note: the `set_interaction_mode` and their associated arg `default_interaction_mode` are being deprecated!
    #       Please use the `set_/interaction_type` ones above with the InteractionType enum instead.
    #       See more details: https://github.com/pytorch/rl/issues/1016
    interaction_mode as exploration_mode,
    interaction_type as exploration_type,
    InteractionType as ExplorationType,
    set_interaction_mode as set_exploration_mode,
    set_interaction_type as set_exploration_type,
)
from tensordict.utils import NestedKey
from torch import nn as nn
from torch.utils._pytree import tree_map
from torchrl._utils import _replace_last, _rng_decorator, logger as torchrl_logger

from torchrl.data.tensor_specs import (
    CompositeSpec,
    NO_DEFAULT,
    TensorSpec,
    UnboundedContinuousTensorSpec,
)
from torchrl.data.utils import check_no_exclusive_keys

__all__ = [
    "exploration_mode",
    "exploration_type",
    "set_exploration_mode",
    "set_exploration_type",
    "ExplorationType",
    "check_env_specs",
    "step_mdp",
    "make_composite_from_td",
    "MarlGroupMapType",
    "check_marl_grouping",
]


ACTION_MASK_ERROR = RuntimeError(
    "An out-of-bounds actions has been provided to an env with an 'action_mask' output."
    " If you are using a custom policy, make sure to take the action mask into account when computing the output."
    " If you are using a default policy, please add the torchrl.envs.transforms.ActionMask transform to your environment."
    "If you are using a ParallelEnv or another batched inventor, "
    "make sure to add the transform to the ParallelEnv (and not to the sub-environments)."
    " For more info on using action masks, see the docs at: "
    "https://pytorch.org/rl/reference/envs.html#environments-with-masked-actions"
)


def _convert_exploration_type(*, exploration_mode, exploration_type):
    if exploration_mode is not None:
        return ExplorationType.from_str(exploration_mode)
    return exploration_type


class _classproperty(property):
    def __get__(self, cls, owner):
        return classmethod(self.fget).__get__(None, owner)()


class _StepMDP:
    """Stateful version of step_mdp.

    Precomputes the list of keys to include and exclude during a call to step_mdp
    to reduce runtime.

    """

    def __init__(
        self,
        env,
        *,
        keep_other: bool = True,
        exclude_reward: bool = True,
        exclude_done: bool = False,
        exclude_action: bool = True,
    ):
        action_keys = env.action_keys
        done_keys = env.done_keys
        reward_keys = env.reward_keys
        observation_keys = env.full_observation_spec.keys(True, True)
        state_keys = env.full_state_spec.keys(True, True)
        self.action_keys = [unravel_key(key) for key in action_keys]
        self.done_keys = [unravel_key(key) for key in done_keys]
        self.reward_keys = [unravel_key(key) for key in reward_keys]
        self.observation_keys = [unravel_key(key) for key in observation_keys]
        self.state_keys = [unravel_key(key) for key in state_keys]

        excluded = set()
        if exclude_reward:
            excluded = excluded.union(self.reward_keys)
        if exclude_done:
            excluded = excluded.union(self.done_keys)
        if exclude_action:
            excluded = excluded.union(self.action_keys)

        self.excluded = [unravel_key(key) for key in excluded]

        self.keep_other = keep_other
        self.exclude_action = exclude_action

        self.keys_from_next = list(self.observation_keys)
        if not exclude_reward:
            self.keys_from_next += self.reward_keys
        if not exclude_done:
            self.keys_from_next += self.done_keys
        self.keys_from_root = []
        if not exclude_action:
            self.keys_from_root += self.action_keys
        if keep_other:
            self.keys_from_root += self.state_keys
        self.keys_from_root = self._repr_key_list_as_tree(self.keys_from_root)
        self.keys_from_next = self._repr_key_list_as_tree(self.keys_from_next)
        self.validated = None

    def validate(self, tensordict):
        if self.validated:
            return True
        if self.validated is None:
            # check that the key set of the tensordict matches what is expected
            expected = (
                self.state_keys
                + self.action_keys
                + self.done_keys
                + self.observation_keys
                + [unravel_key(("next", key)) for key in self.observation_keys]
                + [unravel_key(("next", key)) for key in self.done_keys]
                + [unravel_key(("next", key)) for key in self.reward_keys]
            )
            actual = set(tensordict.keys(True, True))
            self.validated = set(expected) == actual
            if not self.validated:
                warnings.warn(
                    "The expected key set and actual key set differ. "
                    "This will work but with a slower throughput than "
                    "when the specs match exactly the actual key set "
                    "in the data. "
                    f"Expected - Actual keys={set(expected) - actual}, \n"
                    f"Actual - Expected keys={actual- set(expected)}."
                )
        return self.validated

    @staticmethod
    def _repr_key_list_as_tree(key_list):
        """Represents the keys as a tree to facilitate iteration."""
        key_dict = {key: torch.zeros(()) for key in key_list}
        td = TensorDict(key_dict)
        return tree_map(lambda x: None, td.to_dict())

    @classmethod
    def _grab_and_place(
        cls, nested_key_dict: dict, data_in: TensorDictBase, data_out: TensorDictBase
    ):
        for key, subdict in nested_key_dict.items():
            val = data_in._get_str(key, NO_DEFAULT)
            if subdict is not None:
                val_out = data_out._get_str(key, None)
                if val_out is None:
                    val_out = val.empty()
                if isinstance(val, LazyStackedTensorDict):

                    val = LazyStackedTensorDict(
                        *(
                            cls._grab_and_place(subdict, _val, _val_out)
                            for (_val, _val_out) in zip(
                                val.unbind(val.stack_dim),
                                val_out.unbind(val_out.stack_dim),
                            )
                        ),
                        stack_dim=val.stack_dim,
                    )
                else:
                    val = cls._grab_and_place(subdict, val, val_out)
            data_out._set_str(key, val, validated=True, inplace=False)
        return data_out

    def __call__(self, tensordict):
        if isinstance(tensordict, LazyStackedTensorDict):
            out = LazyStackedTensorDict.lazy_stack(
                [self.__call__(td) for td in tensordict.tensordicts],
                tensordict.stack_dim,
            )
            return out

        next_td = tensordict._get_str("next", None)
        out = next_td.empty()
        if self.validate(tensordict):
            self._grab_and_place(self.keys_from_root, tensordict, out)
            self._grab_and_place(self.keys_from_next, next_td, out)
            return out
        else:
            total_key = ()
            if self.keep_other:
                for key in tensordict.keys():
                    if key != "next":
                        _set(tensordict, out, key, total_key, self.excluded)
            elif not self.exclude_action:
                for action_key in self.action_keys:
                    _set_single_key(tensordict, out, action_key)
            for key in next_td.keys():
                _set(next_td, out, key, total_key, self.excluded)
            return out


def step_mdp(
    tensordict: TensorDictBase,
    next_tensordict: TensorDictBase = None,
    keep_other: bool = True,
    exclude_reward: bool = True,
    exclude_done: bool = False,
    exclude_action: bool = True,
    reward_keys: Union[NestedKey, List[NestedKey]] = "reward",
    done_keys: Union[NestedKey, List[NestedKey]] = "done",
    action_keys: Union[NestedKey, List[NestedKey]] = "action",
) -> TensorDictBase:
    """Creates a new tensordict that reflects a step in time of the input tensordict.

    Given a tensordict retrieved after a step, returns the :obj:`"next"` indexed-tensordict.
    The arguments allow for a precise control over what should be kept and what
    should be copied from the ``"next"`` entry. The default behaviour is:
    move the observation entries, reward and done states to the root, exclude
    the current action and keep all extra keys (non-action, non-done, non-reward).

    Args:
        tensordict (TensorDictBase): tensordict with keys to be renamed
        next_tensordict (TensorDictBase, optional): destination tensordict
        keep_other (bool, optional): if ``True``, all keys that do not start with :obj:`'next_'` will be kept.
            Default is ``True``.
        exclude_reward (bool, optional): if ``True``, the :obj:`"reward"` key will be discarded
            from the resulting tensordict. If ``False``, it will be copied (and replaced)
            from the ``"next"`` entry (if present).
            Default is ``True``.
        exclude_done (bool, optional): if ``True``, the :obj:`"done"` key will be discarded
            from the resulting tensordict. If ``False``, it will be copied (and replaced)
            from the ``"next"`` entry (if present).
            Default is ``False``.
        exclude_action (bool, optional): if ``True``, the :obj:`"action"` key will
            be discarded from the resulting tensordict. If ``False``, it will
            be kept in the root tensordict (since it should not be present in
            the ``"next"`` entry).
            Default is ``True``.
        reward_keys (NestedKey or list of NestedKey, optional): the keys where the reward is written. Defaults
            to "reward".
        done_keys (NestedKey or list of NestedKey, optional): the keys where the done is written. Defaults
            to "done".
        action_keys (NestedKey or list of NestedKey, optional): the keys where the action is written. Defaults
            to "action".

    Returns:
         A new tensordict (or next_tensordict) containing the tensors of the t+1 step.

    Examples:
    This funtion allows for this kind of loop to be used:
        >>> from tensordict import TensorDict
        >>> import torch
        >>> td = TensorDict({
        ...     "done": torch.zeros((), dtype=torch.bool),
        ...     "reward": torch.zeros(()),
        ...     "extra": torch.zeros(()),
        ...     "next": TensorDict({
        ...         "done": torch.zeros((), dtype=torch.bool),
        ...         "reward": torch.zeros(()),
        ...         "obs": torch.zeros(()),
        ...     }, []),
        ...     "obs": torch.zeros(()),
        ...     "action": torch.zeros(()),
        ... }, [])
        >>> print(step_mdp(td))
        TensorDict(
            fields={
                done: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.bool, is_shared=False),
                extra: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                obs: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)
        >>> print(step_mdp(td, exclude_done=True))  # "done" is dropped
        TensorDict(
            fields={
                extra: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                obs: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)
        >>> print(step_mdp(td, exclude_reward=False))  # "reward" is kept
        TensorDict(
            fields={
                done: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.bool, is_shared=False),
                extra: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                obs: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                reward: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)
        >>> print(step_mdp(td, exclude_action=False))  # "action" persists at the root
        TensorDict(
            fields={
                action: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                done: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.bool, is_shared=False),
                extra: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                obs: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)
        >>> print(step_mdp(td, keep_other=False))  # "extra" is missing
        TensorDict(
            fields={
                done: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.bool, is_shared=False),
                obs: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)

    """
    if isinstance(tensordict, LazyStackedTensorDict):
        if next_tensordict is not None:
            next_tensordicts = next_tensordict.unbind(tensordict.stack_dim)
        else:
            next_tensordicts = [None] * len(tensordict.tensordicts)
        out = LazyStackedTensorDict.lazy_stack(
            [
                step_mdp(
                    td,
                    next_tensordict=ntd,
                    keep_other=keep_other,
                    exclude_reward=exclude_reward,
                    exclude_done=exclude_done,
                    exclude_action=exclude_action,
                    reward_keys=reward_keys,
                    done_keys=done_keys,
                    action_keys=action_keys,
                )
                for td, ntd in zip(tensordict.tensordicts, next_tensordicts)
            ],
            tensordict.stack_dim,
        )
        if next_tensordict is not None:
            next_tensordict.update(out)
            return next_tensordict
        return out

    if not isinstance(action_keys, list):
        action_keys = [action_keys]
    if not isinstance(done_keys, list):
        done_keys = [done_keys]
    if not isinstance(reward_keys, list):
        reward_keys = [reward_keys]

    excluded = set()
    if exclude_reward:
        excluded = excluded.union(reward_keys)
    if exclude_done:
        excluded = excluded.union(done_keys)
    if exclude_action:
        excluded = excluded.union(action_keys)
    next_td = tensordict.get("next")
    out = next_td.empty()

    total_key = ()
    if keep_other:
        for key in tensordict.keys():
            if key != "next":
                _set(tensordict, out, key, total_key, excluded)
    elif not exclude_action:
        for action_key in action_keys:
            _set_single_key(tensordict, out, action_key)
    for key in next_td.keys():
        _set(next_td, out, key, total_key, excluded)
    if next_tensordict is not None:
        return next_tensordict.update(out)
    else:
        return out


def _set_single_key(
    source: TensorDictBase,
    dest: TensorDictBase,
    key: str | tuple,
    clone: bool = False,
    device=None,
):
    # key should be already unraveled
    if isinstance(key, str):
        key = (key,)
    for k in key:
        try:
            val = source._get_str(k, None)
            if is_tensor_collection(val):
                new_val = dest._get_str(k, None)
                if new_val is None:
                    new_val = val.empty()
                    dest._set_str(k, new_val, inplace=False, validated=True)
                source = val
                dest = new_val
            else:
                if device is not None and val.device != device:
                    val = val.to(device, non_blocking=True)
                elif clone:
                    val = val.clone()
                dest._set_str(k, val, inplace=False, validated=True)
        # This is a temporary solution to understand if a key is heterogeneous
        # while not having performance impact when the exception is not raised
        except RuntimeError as err:
            if re.match(r"Found more than one unique shape in the tensors", str(err)):
                # this is a het key
                for s_td, d_td in zip(source.tensordicts, dest.tensordicts):
                    _set_single_key(s_td, d_td, k, clone=clone, device=device)
                break
            else:
                raise err


def _set(source, dest, key, total_key, excluded):
    total_key = total_key + (key,)
    non_empty = False
    if unravel_key(total_key) not in excluded:
        try:
            val = source.get(key)
            if is_tensor_collection(val):
                # if val is a tensordict we need to copy the structure
                new_val = dest.get(key, None)
                if new_val is None:
                    new_val = val.empty()
                non_empty_local = False
                for subkey in val.keys():
                    non_empty_local = (
                        _set(val, new_val, subkey, total_key, excluded)
                        or non_empty_local
                    )
                if non_empty_local:
                    # dest.set(key, new_val)
                    dest._set_str(key, new_val, inplace=False, validated=True)
                non_empty = non_empty_local
            else:
                non_empty = True
                # dest.set(key, val)
                dest._set_str(key, val, inplace=False, validated=True)
        # This is a temporary solution to understand if a key is heterogeneous
        # while not having performance impact when the exception is not raised
        except RuntimeError as err:
            if re.match(r"Found more than one unique shape in the tensors", str(err)):
                # this is a het key
                non_empty_local = False
                for s_td, d_td in zip(source.tensordicts, dest.tensordicts):
                    non_empty_local = (
                        _set(s_td, d_td, key, total_key, excluded) or non_empty_local
                    )
                non_empty = non_empty_local
            else:
                raise err

    return non_empty


def get_available_libraries():
    """Returns all the supported libraries."""
    return SUPPORTED_LIBRARIES


def _check_gym():
    """Returns True if the gym library is installed."""
    return importlib.util.find_spec("gym") is not None


def _check_gym_atari():
    """Returns True if the gym library is installed and atari envs can be found."""
    if not _check_gym():
        return False
    return importlib.util.find_spec("atari-py") is not None


def _check_mario():
    """Returns True if the "gym-super-mario-bros" library is installed."""
    return importlib.util.find_spec("gym-super-mario-bros") is not None


def _check_dmcontrol():
    """Returns True if the "dm-control" library is installed."""
    return importlib.util.find_spec("dm_control") is not None


def _check_dmlab():
    """Returns True if the "deepmind-lab" library is installed."""
    return importlib.util.find_spec("deepmind_lab") is not None


SUPPORTED_LIBRARIES = {
    "gym": _check_gym(),  # OpenAI
    "gym[atari]": _check_gym_atari(),  #
    "dm_control": _check_dmcontrol(),
    "habitat": None,
    "gym-super-mario-bros": _check_mario(),
    # "vizdoom": None,  # gym based, https://github.com/mwydmuch/ViZDoom
    # "openspiel": None,  # DM, https://github.com/deepmind/open_spiel
    # "pysc2": None,  # DM, https://github.com/deepmind/pysc2
    # "deepmind_lab": _check_dmlab(),
    # DM, https://github.com/deepmind/lab, https://github.com/deepmind/lab/tree/master/python/pip_package
    # "serpent.ai": None,  # https://github.com/SerpentAI/SerpentAI
    # "gfootball": None,  # 2.8k G, https://github.com/google-research/football
    # DM, https://github.com/deepmind/dm_control
    # FB, https://github.com/facebookresearch/habitat-sim
    # "meta-world": None,  # https://github.com/rlworkgroup/metaworld
    # "minerl": None,  # https://github.com/minerllabs/minerl
    # "multi-agent-emergence-environments": None,
    # OpenAI, https://github.com/openai/multi-agent-emergence-environments
    # "procgen": None,  # OpenAI, https://github.com/openai/procgen
    # "pybullet": None,  # https://github.com/benelot/pybullet-gym
    # "realworld_rl_suite": None,
    # G, https://github.com/google-research/realworldrl_suite
    # "rlcard": None,  # https://github.com/datamllab/rlcard
    # "screeps": None,  # https://github.com/screeps/screeps
    # "ml-agents": None,
}


def _per_level_env_check(data0, data1, check_dtype):
    """Checks shape and dtype of two tensordicts, accounting for lazy stacks."""
    if isinstance(data0, LazyStackedTensorDict) and isinstance(
        data1, LazyStackedTensorDict
    ):
        if data0.stack_dim != data1.stack_dim:
            raise AssertionError(f"Stack dimension mismatch: {data0} vs {data1}.")
        for _data0, _data1 in zip(data0.tensordicts, data1.tensordicts):
            _per_level_env_check(_data0, _data1, check_dtype=check_dtype)
        return
    else:
        keys0 = set(data0.keys())
        keys1 = set(data1.keys())
        if keys0 != keys1:
            raise AssertionError(f"Keys mismatch: {keys0} vs {keys1}")
        for key in keys0:
            _data0 = data0[key]
            _data1 = data1[key]
            if _data0.shape != _data1.shape:
                raise AssertionError(
                    f"The shapes of the real and fake tensordict don't match for key {key}. "
                    f"Got fake={_data0.shape} and real={_data1.shape}."
                )
            if isinstance(_data0, TensorDictBase):
                _per_level_env_check(_data0, _data1, check_dtype=check_dtype)
            else:
                if check_dtype and (_data0.dtype != _data1.dtype):
                    raise AssertionError(
                        f"The dtypes of the real and fake tensordict don't match for key {key}. "
                        f"Got fake={_data0.dtype} and real={_data1.dtype}."
                    )


def check_env_specs(
    env, return_contiguous=True, check_dtype=True, seed: int | None = None
):
    """Tests an environment specs against the results of short rollout.

    This test function should be used as a sanity check for an env wrapped with
    torchrl's EnvBase subclasses: any discrepancy between the expected data and
    the data collected should raise an assertion error.

    A broken environment spec will likely make it impossible to use parallel
    environments.

    Args:
        env (EnvBase): the env for which the specs have to be checked against data.
        return_contiguous (bool, optional): if ``True``, the random rollout will be called with
            return_contiguous=True. This will fail in some cases (e.g. heterogeneous shapes
            of inputs/outputs). Defaults to True.
        check_dtype (bool, optional): if False, dtype checks will be skipped.
            Defaults to True.
        seed (int, optional): for reproducibility, a seed can be set.
            The seed will be set in pytorch temporarily, then the RNG state will
            be reverted to what it was before. For the env, we set the seed but since
            setting the rng state back to what is was isn't a feature of most environment,
            we leave it to the user to accomplish that.
            Defaults to ``None``.

    Caution: this function resets the env seed. It should be used "offline" to
    check that an env is adequately constructed, but it may affect the seeding
    of an experiment and as such should be kept out of training scripts.

    """
    if seed is not None:
        device = (
            env.device if env.device is not None and env.device.type == "cuda" else None
        )
        with _rng_decorator(seed, device=device):
            env.set_seed(seed)
            return check_env_specs(
                env, return_contiguous=return_contiguous, check_dtype=check_dtype
            )

    fake_tensordict = env.fake_tensordict()
    real_tensordict = env.rollout(3, return_contiguous=return_contiguous)

    if return_contiguous:
        fake_tensordict = fake_tensordict.unsqueeze(real_tensordict.batch_dims - 1)
        fake_tensordict = fake_tensordict.expand(*real_tensordict.shape)
    else:
        fake_tensordict = LazyStackedTensorDict.lazy_stack(
            [fake_tensordict.clone() for _ in range(3)], -1
        )
    # eliminate empty containers
    fake_tensordict_select = fake_tensordict.select(*fake_tensordict.keys(True, True))
    real_tensordict_select = real_tensordict.select(*real_tensordict.keys(True, True))
    # check keys
    fake_tensordict_keys = set(fake_tensordict.keys(True, True))
    real_tensordict_keys = set(real_tensordict.keys(True, True))
    if fake_tensordict_keys != real_tensordict_keys:
        raise AssertionError(
            f"""The keys of the specs and data do not match:
    - List of keys present in real but not in fake: {real_tensordict_keys-fake_tensordict_keys},
    - List of keys present in fake but not in real: {fake_tensordict_keys-real_tensordict_keys}.
"""
        )
    if (
        fake_tensordict_select.apply(lambda x: torch.zeros_like(x))
        != real_tensordict_select.apply(lambda x: torch.zeros_like(x))
    ).any():
        raise AssertionError(
            "zeroing the two tensordicts did not make them identical. "
            f"Check for discrepancies:\nFake=\n{fake_tensordict}\nReal=\n{real_tensordict}"
        )

    # Checks shapes and eventually dtypes of keys at all nesting levels
    _per_level_env_check(
        fake_tensordict_select, real_tensordict_select, check_dtype=check_dtype
    )

    # Check specs
    last_td = real_tensordict[..., -1]
    last_td = env.rand_action(last_td)
    full_action_spec = env.input_spec["full_action_spec"]
    full_state_spec = env.input_spec["full_state_spec"]
    full_observation_spec = env.output_spec["full_observation_spec"]
    full_reward_spec = env.output_spec["full_reward_spec"]
    full_done_spec = env.output_spec["full_done_spec"]
    for name, spec in (
        ("action", full_action_spec),
        ("state", full_state_spec),
        ("done", full_done_spec),
        ("obs", full_observation_spec),
    ):
        if not check_no_exclusive_keys(spec):
            raise AssertionError(
                "It appears you are using some LazyStackedCompositeSpecs with exclusive keys "
                "(keys present in some but not all of the stacked specs). To use such heterogeneous specs, "
                "you will need to first pass your stack through `torchrl.data.consolidate_spec`."
            )
        if spec is None:
            spec = CompositeSpec(shape=env.batch_size, device=env.device)
        td = last_td.select(*spec.keys(True, True), strict=True)
        if not spec.is_in(td):
            raise AssertionError(
                f"spec check failed at root for spec {name}={spec} and data {td}."
            )
    for name, spec in (
        ("reward", full_reward_spec),
        ("done", full_done_spec),
        ("obs", full_observation_spec),
    ):
        if spec is None:
            spec = CompositeSpec(shape=env.batch_size, device=env.device)
        td = last_td.get("next").select(*spec.keys(True, True), strict=True)
        if not spec.is_in(td):
            raise AssertionError(
                f"spec check failed at root for spec {name}={spec} and data {td}."
            )

    torchrl_logger.info("check_env_specs succeeded!")


def _selective_unsqueeze(tensor: torch.Tensor, batch_size: torch.Size, dim: int = -1):
    shape_len = len(tensor.shape)

    if shape_len < len(batch_size):
        raise RuntimeError(
            f"Tensor has less dims than batch_size. shape:{tensor.shape}, batch_size: {batch_size}"
        )
    if tensor.shape[: len(batch_size)] != batch_size:
        raise RuntimeError(
            f"Tensor does not have given batch_size. shape:{tensor.shape}, batch_size: {batch_size}"
        )

    if shape_len == len(batch_size):
        return tensor.unsqueeze(dim=dim)
    return tensor


def _sort_keys(element):
    if isinstance(element, tuple):
        element = unravel_key(element)
        return "_-|-_".join(element)
    return element


def make_composite_from_td(data):
    """Creates a CompositeSpec instance from a tensordict, assuming all values are unbounded.

    Args:
        data (tensordict.TensorDict): a tensordict to be mapped onto a CompositeSpec.

    Examples:
        >>> from tensordict import TensorDict
        >>> data = TensorDict({
        ...     "obs": torch.randn(3),
        ...     "action": torch.zeros(2, dtype=torch.int),
        ...     "next": {"obs": torch.randn(3), "reward": torch.randn(1)}
        ... }, [])
        >>> spec = make_composite_from_td(data)
        >>> print(spec)
        CompositeSpec(
            obs: UnboundedContinuousTensorSpec(
                 shape=torch.Size([3]), space=None, device=cpu, dtype=torch.float32, domain=continuous),
            action: UnboundedContinuousTensorSpec(
                 shape=torch.Size([2]), space=None, device=cpu, dtype=torch.int32, domain=continuous),
            next: CompositeSpec(
                obs: UnboundedContinuousTensorSpec(
                     shape=torch.Size([3]), space=None, device=cpu, dtype=torch.float32, domain=continuous),
                reward: UnboundedContinuousTensorSpec(
                     shape=torch.Size([1]), space=ContinuousBox(low=Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, contiguous=True), high=Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, contiguous=True)), device=cpu, dtype=torch.float32, domain=continuous), device=cpu, shape=torch.Size([])), device=cpu, shape=torch.Size([]))
        >>> assert (spec.zero() == data.zero_()).all()
    """
    # custom funtion to convert a tensordict in a similar spec structure
    # of unbounded values.
    composite = CompositeSpec(
        {
            key: make_composite_from_td(tensor)
            if isinstance(tensor, TensorDictBase)
            else UnboundedContinuousTensorSpec(
                dtype=tensor.dtype,
                device=tensor.device,
                shape=tensor.shape if tensor.shape else [1],
            )
            for key, tensor in data.items()
        },
        shape=data.shape,
    )
    return composite


@contextlib.contextmanager
def clear_mpi_env_vars():
    """Clears the MPI of environment variables.

    `from mpi4py import MPI` will call `MPI_Init` by default.
    If the child process has MPI environment variables, MPI will think that the child process
    is an MPI process just like the parent and do bad things such as hang.

    This context manager is a hacky way to clear those environment variables
    temporarily such as when we are starting multiprocessing Processes.

    Yields:
        Yields for the context manager
    """
    removed_environment = {}
    for k, v in list(os.environ.items()):
        for prefix in ["OMPI_", "PMI_"]:
            if k.startswith(prefix):
                removed_environment[k] = v
                del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(removed_environment)


class MarlGroupMapType(Enum):
    """Marl Group Map Type.

    As a feature of torchrl multiagent, you are able to control the grouping of agents in your environment.
    You can group agents together (stacking their tensors) to leverage vectorization when passing them through the same
    neural network. You can split agents in different groups where they are heterogenous or should be processed by
    different neural networks. To group, you just need to pass a ``group_map`` at env constructiuon time.

    Otherwise, you can choose one of the premade grouping strategies from this class.

    - With ``group_map=MarlGroupMapType.ALL_IN_ONE_GROUP`` and
      agents ``["agent_0", "agent_1", "agent_2", "agent_3"]``,
      the tensordicts coming and going from your environment will look
      something like:

        >>> print(env.rand_action(env.reset()))
        TensorDict(
            fields={
                agents: TensorDict(
                    fields={
                        action: Tensor(shape=torch.Size([4, 9]), device=cpu, dtype=torch.int64, is_shared=False),
                        done: Tensor(shape=torch.Size([4, 1]), device=cpu, dtype=torch.bool, is_shared=False),
                        observation: Tensor(shape=torch.Size([4, 3, 3, 2]), device=cpu, dtype=torch.int8, is_shared=False)},
                    batch_size=torch.Size([4]))},
            batch_size=torch.Size([]))
        >>> print(env.group_map)
        {"agents": ["agent_0", "agent_1", "agent_2", "agent_3]}

    - With ``group_map=MarlGroupMapType.ONE_GROUP_PER_AGENT`` and
      agents ``["agent_0", "agent_1", "agent_2", "agent_3"]``,
      the tensordicts coming and going from your environment will look
      something like:

        >>> print(env.rand_action(env.reset()))
        TensorDict(
            fields={
                agent_0: TensorDict(
                    fields={
                        action: Tensor(shape=torch.Size([9]), device=cpu, dtype=torch.int64, is_shared=False),
                        done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                        observation: Tensor(shape=torch.Size([3, 3, 2]), device=cpu, dtype=torch.int8, is_shared=False)},
                    batch_size=torch.Size([]))},
                agent_1: TensorDict(
                    fields={
                        action: Tensor(shape=torch.Size([9]), device=cpu, dtype=torch.int64, is_shared=False),
                        done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                        observation: Tensor(shape=torch.Size([3, 3, 2]), device=cpu, dtype=torch.int8, is_shared=False)},
                    batch_size=torch.Size([]))},
                agent_2: TensorDict(
                    fields={
                        action: Tensor(shape=torch.Size([9]), device=cpu, dtype=torch.int64, is_shared=False),
                        done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                        observation: Tensor(shape=torch.Size([3, 3, 2]), device=cpu, dtype=torch.int8, is_shared=False)},
                    batch_size=torch.Size([]))},
                agent_3: TensorDict(
                    fields={
                        action: Tensor(shape=torch.Size([9]), device=cpu, dtype=torch.int64, is_shared=False),
                        done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                        observation: Tensor(shape=torch.Size([3, 3, 2]), device=cpu, dtype=torch.int8, is_shared=False)},
                    batch_size=torch.Size([]))},
            batch_size=torch.Size([]))
        >>> print(env.group_map)
        {"agent_0": ["agent_0"], "agent_1": ["agent_1"], "agent_2": ["agent_2"], "agent_3": ["agent_3"]}
    """

    ALL_IN_ONE_GROUP = 1
    ONE_GROUP_PER_AGENT = 2

    def get_group_map(self, agent_names: List[str]):
        if self == MarlGroupMapType.ALL_IN_ONE_GROUP:
            return {"agents": agent_names}
        elif self == MarlGroupMapType.ONE_GROUP_PER_AGENT:
            return {agent_name: [agent_name] for agent_name in agent_names}


def check_marl_grouping(group_map: Dict[str, List[str]], agent_names: List[str]):
    """Check MARL group map.

    Performs checks on the group map of a marl environment to assess its validity.
    Raises an error in cas of an invalid group_map.

    Args:
        group_map (Dict[str, List[str]]): the group map mapping group names to list of agent names in the group
        agent_names (List[str]): a list of all the agent names in the environment4

    Examples:
        >>> from torchrl.envs.utils import MarlGroupMapType, check_marl_grouping
        >>> agent_names = ["agent_0", "agent_1", "agent_2"]
        >>> check_marl_grouping(MarlGroupMapType.ALL_IN_ONE_GROUP.get_group_map(agent_names), agent_names)

    """
    n_agents = len(agent_names)
    if n_agents == 0:
        raise ValueError("No agents passed")
    if len(set(agent_names)) != n_agents:
        raise ValueError("There are agents with the same name")
    if len(group_map.keys()) > n_agents:
        raise ValueError(
            f"Number of groups {len(group_map.keys())} greater than number of agents {n_agents}"
        )
    found_agents = {agent_name: False for agent_name in agent_names}
    for group_name, group in group_map.items():
        if not len(group):
            raise ValueError(f"Group {group_name} is empty")
        for agent_name in group:
            if agent_name not in found_agents:
                raise ValueError(f"Agent {agent_name} not present in environment")
            if not found_agents[agent_name]:
                found_agents[agent_name] = True
            else:
                raise ValueError(f"Agent {agent_name} present more than once")
    for agent_name, found in found_agents.items():
        if not found:
            raise ValueError(f"Agent {agent_name} not found in any group")


def _terminated_or_truncated(
    data: TensorDictBase,
    full_done_spec: TensorSpec | None = None,
    key: str | None = "_reset",
    write_full_false: bool = False,
) -> bool:
    """Reads the done / terminated / truncated keys within a tensordict, and writes a new tensor where the values of both signals are aggregated.

    The modification occurs in-place within the TensorDict instance provided.
    This function can be used to compute the `"_reset"` signals in batched
    or multiagent settings, hence the default name of the output key.

    Args:
        data (TensorDictBase): the input data, generally resulting from a call
            to :meth:`~torchrl.envs.EnvBase.step`.
        full_done_spec (TensorSpec, optional): the done_spec from the env,
            indicating where the done leaves have to be found.
            If not provided, the default
            ``"done"``, ``"terminated"`` and ``"truncated"`` entries will be
            searched for in the data.
        key (NestedKey, optional): where the aggregated result should be written.
            If ``None``, then the function will not write any key but just output
            whether any of the done values was true.
            .. note:: if a value is already present for the ``key`` entry,
                the previous value will prevail and no update will be achieved.
        write_full_false (bool, optional): if ``True``, the reset keys will be
            written even if the output is ``False`` (ie, no done is ``True``
            in the provided data structure).
            Defaults to ``False``.

    Returns: a boolean value indicating whether any of the done states found in the data
        contained a ``True``.

    Examples:
        >>> from torchrl.data.tensor_specs import DiscreteTensorSpec
        >>> from tensordict import TensorDict
        >>> spec = CompositeSpec(
        ...     done=DiscreteTensorSpec(2, dtype=torch.bool),
        ...     truncated=DiscreteTensorSpec(2, dtype=torch.bool),
        ...     nested=CompositeSpec(
        ...         done=DiscreteTensorSpec(2, dtype=torch.bool),
        ...         truncated=DiscreteTensorSpec(2, dtype=torch.bool),
        ...     )
        ... )
        >>> data = TensorDict({
        ...     "done": True, "truncated": False,
        ...     "nested": {"done": False, "truncated": True}},
        ...     batch_size=[]
        ... )
        >>> data = _terminated_or_truncated(data, spec)
        >>> print(data["_reset"])
        tensor(True)
        >>> print(data["nested", "_reset"])
        tensor(True)
    """
    list_of_keys = []

    def inner_terminated_or_truncated(data, full_done_spec, key, curr_done_key=()):
        any_eot = False
        aggregate = None
        if full_done_spec is None:
            tds = {}
            found_leaf = 0
            for eot_key, item in data.items():
                if eot_key in ("terminated", "truncated", "done"):
                    done = item
                    if aggregate is None:
                        aggregate = False
                    aggregate = aggregate | done
                    found_leaf += 1
                elif isinstance(item, TensorDictBase):
                    tds[eot_key] = item
            # The done signals in a root td prevail over done in the leaves
            if tds:
                for eot_key, item in tds.items():
                    any_eot_td = inner_terminated_or_truncated(
                        data=item,
                        full_done_spec=None,
                        key=key,
                        curr_done_key=curr_done_key + (eot_key,),
                    )
                    if not found_leaf:
                        any_eot = any_eot | any_eot_td
        else:
            composite_spec = {}
            found_leaf = 0
            for eot_key, item in full_done_spec.items():
                if isinstance(item, CompositeSpec):
                    composite_spec[eot_key] = item
                else:
                    found_leaf += 1
                    stop = data.get(eot_key, None)
                    if stop is None:
                        stop = torch.zeros(
                            (*data.shape, 1), dtype=torch.bool, device=data.device
                        )
                    if aggregate is None:
                        aggregate = False
                    aggregate = aggregate | stop
            # The done signals in a root td prevail over done in the leaves
            if composite_spec:
                for eot_key, item in composite_spec.items():
                    any_eot_td = inner_terminated_or_truncated(
                        data=data.get(eot_key),
                        full_done_spec=item,
                        key=key,
                        curr_done_key=curr_done_key + (eot_key,),
                    )
                    if not found_leaf:
                        any_eot = any_eot_td | any_eot

        if aggregate is not None:
            if key is not None:
                if aggregate.ndim > data.ndim:
                    # accounts for trailing singleton dim in done.
                    # _reset is always expanded on the right if needed so this can only be useful
                    aggregate = aggregate.squeeze(-1)
                data.set(key, aggregate)
                list_of_keys.append(curr_done_key + (key,))
            any_eot = any_eot | aggregate.any()
        return any_eot

    any_eot = inner_terminated_or_truncated(data, full_done_spec, key)
    if not any_eot and not write_full_false:
        # remove the list of reset keys
        data.exclude(*list_of_keys, inplace=True)
    return any_eot


def terminated_or_truncated(
    data: TensorDictBase,
    full_done_spec: TensorSpec | None = None,
    key: str = "_reset",
    write_full_false: bool = False,
) -> bool:
    """Reads the done / terminated / truncated keys within a tensordict, and writes a new tensor where the values of both signals are aggregated.

    The modification occurs in-place within the TensorDict instance provided.
    This function can be used to compute the `"_reset"` signals in batched
    or multiagent settings, hence the default name of the output key.

    Args:
        data (TensorDictBase): the input data, generally resulting from a call
            to :meth:`~torchrl.envs.EnvBase.step`.
        full_done_spec (TensorSpec, optional): the done_spec from the env,
            indicating where the done leaves have to be found.
            If not provided, the default
            ``"done"``, ``"terminated"`` and ``"truncated"`` entries will be
            searched for in the data.
        key (NestedKey, optional): where the aggregated result should be written.
            If ``None``, then the function will not write any key but just output
            whether any of the done values was true.
            .. note:: if a value is already present for the ``key`` entry,
                the previous value will prevail and no update will be achieved.
        write_full_false (bool, optional): if ``True``, the reset keys will be
            written even if the output is ``False`` (ie, no done is ``True``
            in the provided data structure).
            Defaults to ``False``.

    Returns: a boolean value indicating whether any of the done states found in the data
        contained a ``True``.

    Examples:
        >>> from torchrl.data.tensor_specs import DiscreteTensorSpec
        >>> from tensordict import TensorDict
        >>> spec = CompositeSpec(
        ...     done=DiscreteTensorSpec(2, dtype=torch.bool),
        ...     truncated=DiscreteTensorSpec(2, dtype=torch.bool),
        ...     nested=CompositeSpec(
        ...         done=DiscreteTensorSpec(2, dtype=torch.bool),
        ...         truncated=DiscreteTensorSpec(2, dtype=torch.bool),
        ...     )
        ... )
        >>> data = TensorDict({
        ...     "done": True, "truncated": False,
        ...     "nested": {"done": False, "truncated": True}},
        ...     batch_size=[]
        ... )
        >>> data = _terminated_or_truncated(data, spec)
        >>> print(data["_reset"])
        tensor(True)
        >>> print(data["nested", "_reset"])
        tensor(True)
    """
    list_of_keys = []

    def inner_terminated_or_truncated(data, full_done_spec, key, curr_done_key=()):
        any_eot = False
        aggregate = None
        if full_done_spec is None:
            for eot_key, item in data.items():
                if eot_key == "done":
                    done = data.get(eot_key, None)
                    if done is None:
                        done = torch.zeros(
                            (*data.shape, 1), dtype=torch.bool, device=data.device
                        )
                    if aggregate is None:
                        aggregate = torch.tensor(False, device=done.device)
                    aggregate = aggregate | done
                elif eot_key in ("terminated", "truncated"):
                    done = data.get(eot_key, None)
                    if done is None:
                        done = torch.zeros(
                            (*data.shape, 1), dtype=torch.bool, device=data.device
                        )
                    if aggregate is None:
                        aggregate = torch.tensor(False, device=done.device)
                    aggregate = aggregate | done
                elif isinstance(item, TensorDictBase):
                    any_eot = any_eot | inner_terminated_or_truncated(
                        data=item,
                        full_done_spec=None,
                        key=key,
                        curr_done_key=curr_done_key + (eot_key,),
                    )
        else:
            for eot_key, item in full_done_spec.items():
                if isinstance(item, CompositeSpec):
                    any_eot = any_eot | inner_terminated_or_truncated(
                        data=data.get(eot_key),
                        full_done_spec=item,
                        key=key,
                        curr_done_key=curr_done_key + (eot_key,),
                    )
                else:
                    sop = data.get(eot_key, None)
                    if sop is None:
                        sop = torch.zeros(
                            (*data.shape, 1), dtype=torch.bool, device=data.device
                        )
                    if aggregate is None:
                        aggregate = torch.tensor(False, device=sop.device)
                    aggregate = aggregate | sop
        if aggregate is not None:
            if key is not None:
                data.set(key, aggregate)
                list_of_keys.append(curr_done_key + (key,))
            any_eot = any_eot | aggregate.any()
        return any_eot

    any_eot = inner_terminated_or_truncated(data, full_done_spec, key)
    if not any_eot and not write_full_false:
        # remove the list of reset keys
        data.exclude(*list_of_keys, inplace=True)
    return any_eot


PARTIAL_MISSING_ERR = "Some reset keys were present but not all. Either all the `'_reset'` entries must be present, or none."


def _aggregate_end_of_traj(
    data: TensorDictBase, reset_keys=None, done_keys=None
) -> torch.Tensor:
    # goes through the tensordict and brings the _reset information to
    # a boolean tensor of the shape of the tensordict.
    batch_size = data.batch_size
    n = len(batch_size)
    if done_keys is not None and reset_keys is None:
        reset_keys = {_replace_last(key, "done") for key in done_keys}
    if reset_keys is not None:
        reset = False
        has_missing = None
        for key in reset_keys:
            local_reset = data.get(key, None)
            if local_reset is None:
                if has_missing is False:
                    raise ValueError(PARTIAL_MISSING_ERR)
                has_missing = True
                continue
            elif has_missing:
                raise ValueError(PARTIAL_MISSING_ERR)
            has_missing = False
            if local_reset.ndim > n:
                local_reset = local_reset.flatten(n, local_reset.ndim - 1)
                local_reset = local_reset.any(-1)
            reset = reset | local_reset
        if has_missing:
            return torch.ones(batch_size, dtype=torch.bool, device=data.device)
        return reset

    reset = torch.tensor(False, device=data.device)

    def skim_through(td, reset=reset):
        for key in td.keys():
            if key == "_reset":
                local_reset = td.get(key)
                if local_reset.ndim > n:
                    local_reset = local_reset.flatten(n, local_reset.ndim - 1)
                    local_reset = local_reset.any(-1)
                reset = reset | local_reset
            # we need to check the entry class without getting the value,
            # because some lazy tensordicts may prevent calls to items().
            # This introduces some slight overhead as when we encounter a
            # tensordict item, we'll need to get it twice.
            elif is_tensor_collection(td.entry_class(key)):
                value = td.get(key)
                reset = skim_through(value, reset=reset)
        return reset

    reset = skim_through(data)
    return reset


def _update_during_reset(
    tensordict_reset: TensorDictBase,
    tensordict: TensorDictBase,
    reset_keys: List[NestedKey],
):
    """Updates the input tensordict with the reset data, based on the reset keys."""
    roots = set()
    for reset_key in reset_keys:
        # get the node of the reset key
        if isinstance(reset_key, tuple):
            # the reset key *must* have gone through unravel_key
            # we don't test it to avoid induced overhead
            node_key = reset_key[:-1]
            node_reset = tensordict_reset.get(node_key)
            node = tensordict.get(node_key)
            reset_key_tuple = reset_key
        else:
            node_reset = tensordict_reset
            node = tensordict
            reset_key_tuple = (reset_key,)
        # get the reset signal
        reset = tensordict.pop(reset_key, None)

        # check if this reset should be ignored -- this happens whenever the a
        # root node has already been updated
        root = () if isinstance(reset_key, str) else reset_key[:-1]
        processed = any(reset_key_tuple[: len(x)] == x for x in roots)
        roots.add(root)
        if processed:
            continue

        if reset is None or reset.all():
            # perform simple update, at a single level.
            # by contract, a reset signal at one level cannot
            # be followed by other resets at nested levels, so it's safe to
            # simply update
            node.update(node_reset)
        else:
            # there can be two cases: (1) the key is present in both tds,
            # in which case we use the reset mask to update
            # (2) the key is not present in the input tensordict, in which
            # case we just return the data

            # empty tensordicts won't be returned
            if reset.ndim > node.ndim:
                reset = reset.flatten(node.ndim, reset.ndim - 1)
                reset = reset.any(-1)
            reset = reset.reshape(node.shape)
            # node.update(node.where(~reset, other=node_reset, pad=0))
            node.where(~reset, other=node_reset, out=node, pad=0)
    return tensordict


def _repr_by_depth(key):
    """Used to sort keys based on nesting level."""
    key = unravel_key(key)
    if isinstance(key, str):
        return (0, key)
    else:
        return (len(key) - 1, ".".join(key))


def _make_compatible_policy(policy, observation_spec, env=None, fast_wrap=False):
    if policy is None:
        if env is None:
            raise ValueError(
                "env must be provided to _get_policy_and_device if policy is None"
            )
        policy = RandomPolicy(env.input_spec["full_action_spec"])
    # make sure policy is an nn.Module
    policy = _NonParametricPolicyWrapper(policy)
    if not _policy_is_tensordict_compatible(policy):
        # policy is a nn.Module that doesn't operate on tensordicts directly
        # so we attempt to auto-wrap policy with TensorDictModule
        if observation_spec is None:
            raise ValueError(
                "Unable to read observation_spec from the environment. This is "
                "required to check compatibility of the environment and policy "
                "since the policy is a nn.Module that operates on tensors "
                "rather than a TensorDictModule or a nn.Module that accepts a "
                "TensorDict as input and defines in_keys and out_keys."
            )

        try:
            sig = policy.forward.__signature__
        except AttributeError:
            sig = inspect.signature(policy.forward)
        # we check if all the mandatory params are there
        params = list(sig.parameters.keys())
        if (
            set(sig.parameters) == {"tensordict"}
            or set(sig.parameters) == {"td"}
            or (
                len(params) == 1
                and is_tensor_collection(sig.parameters[params[0]].annotation)
            )
        ):
            return policy
        if fast_wrap:
            in_keys = list(observation_spec.keys())
            out_keys = list(env.action_keys)
            return TensorDictModule(policy, in_keys=in_keys, out_keys=out_keys)

        required_kwargs = {
            str(k) for k, p in sig.parameters.items() if p.default is inspect._empty
        }
        next_observation = {
            key: value for key, value in observation_spec.rand().items()
        }
        if not required_kwargs.difference(set(next_observation)):
            in_keys = [str(k) for k in sig.parameters if k in next_observation]
            if env is None:
                out_keys = ["action"]
            else:
                out_keys = list(env.action_keys)
            for p in policy.parameters():
                policy_device = p.device
                break
            else:
                policy_device = None
            if policy_device:
                next_observation = tree_map(
                    lambda x: x.to(policy_device), next_observation
                )

            output = policy(**next_observation)

            if isinstance(output, tuple):
                out_keys.extend(f"output{i + 1}" for i in range(len(output) - 1))

            policy = TensorDictModule(policy, in_keys=in_keys, out_keys=out_keys)
        else:
            raise TypeError(
                f"""Arguments to policy.forward are incompatible with entries in
    env.observation_spec (got incongruent signatures: fun signature is {set(sig.parameters)} vs specs {set(next_observation)}).
    If you want TorchRL to automatically wrap your policy with a TensorDictModule
    then the arguments to policy.forward must correspond one-to-one with entries
    in env.observation_spec.
    For more complex behaviour and more control you can consider writing your
    own TensorDictModule.
    Check the collector documentation to know more about accepted policies.
    """
            )
    return policy


def _policy_is_tensordict_compatible(policy: nn.Module):
    if isinstance(policy, _NonParametricPolicyWrapper) and isinstance(
        policy.policy, RandomPolicy
    ):
        return True

    if isinstance(policy, TensorDictModuleBase):
        return True

    sig = inspect.signature(policy.forward)

    if (
        len(sig.parameters) == 1
        and hasattr(policy, "in_keys")
        and hasattr(policy, "out_keys")
    ):
        raise RuntimeError(
            "Passing a policy that is not a tensordict.nn.TensorDictModuleBase subclass but has in_keys and out_keys "
            "is deprecated. Users should inherit from this class (which "
            "has very few restrictions) to make the experience smoother. "
            "Simply change your policy from `class Policy(nn.Module)` to `Policy(tensordict.nn.TensorDictModuleBase)` "
            "and this error should disappear.",
        )
    elif not hasattr(policy, "in_keys") and not hasattr(policy, "out_keys"):
        # if it's not a TensorDictModule, and in_keys and out_keys are not defined then
        # we assume no TensorDict compatibility and will try to wrap it.
        return False

    # if in_keys or out_keys were defined but policy is not a TensorDictModule or
    # accepts multiple arguments then it's likely the user is trying to do something
    # that will have undetermined behaviour, we raise an error
    raise TypeError(
        "Received a policy that defines in_keys or out_keys and also expects multiple "
        "arguments to policy.forward. If the policy is compatible with TensorDict, it "
        "should take a single argument of type TensorDict to policy.forward and define "
        "both in_keys and out_keys. Alternatively, policy.forward can accept "
        "arbitrarily many tensor inputs and leave in_keys and out_keys undefined and "
        "TorchRL will attempt to automatically wrap the policy with a TensorDictModule."
    )


class RandomPolicy:
    """A random policy for data collectors.

    This is a wrapper around the action_spec.rand method.

    Args:
        action_spec: TensorSpec object describing the action specs

    Examples:
        >>> from tensordict import TensorDict
        >>> from torchrl.data.tensor_specs import BoundedTensorSpec
        >>> action_spec = BoundedTensorSpec(-torch.ones(3), torch.ones(3))
        >>> actor = RandomPolicy(action_spec=action_spec)
        >>> td = actor(TensorDict({}, batch_size=[])) # selects a random action in the cube [-1; 1]
    """

    def __init__(self, action_spec: TensorSpec, action_key: NestedKey = "action"):
        super().__init__()
        self.action_spec = action_spec.clone()
        self.action_key = action_key

    def __call__(self, td: TensorDictBase) -> TensorDictBase:
        if isinstance(self.action_spec, CompositeSpec):
            return td.update(self.action_spec.rand())
        else:
            return td.set(self.action_key, self.action_spec.rand())


class _PolicyMetaClass(abc.ABCMeta):
    def __call__(cls, *args, **kwargs):
        # no kwargs
        if isinstance(args[0], nn.Module):
            return args[0]
        return super().__call__(*args)


class _NonParametricPolicyWrapper(nn.Module, metaclass=_PolicyMetaClass):
    """A wrapper for non-parametric policies."""

    def __init__(self, policy):
        super().__init__()
        self.policy = policy

    @property
    def forward(self):
        forward = self.__dict__.get("_forward", None)
        if forward is None:

            @functools.wraps(self.policy)
            def forward(*input, **kwargs):
                return self.policy.__call__(*input, **kwargs)

            self.__dict__["_forward"] = forward
        return forward

    def __getattr__(self, attr: str) -> Any:
        if attr in self.__dir__():
            return self.__getattribute__(
                attr
            )  # make sure that appropriate exceptions are raised

        elif attr.startswith("__"):
            raise AttributeError(
                "passing built-in private methods is "
                f"not permitted with type {type(self)}. "
                f"Got attribute {attr}."
            )

        elif "policy" in self.__dir__():
            policy = self.__getattribute__("policy")
            return getattr(policy, attr)
        try:
            super().__getattr__(attr)
        except Exception:
            raise AttributeError(
                f"policy not set in {self.__class__.__name__}, cannot access {attr}."
            )
