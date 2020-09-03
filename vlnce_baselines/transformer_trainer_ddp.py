import copy
import gc
import json
import os
import random
import time
import warnings
from collections import defaultdict
from typing import Dict
import matplotlib.pyplot as plt
import scipy.misc

import lmdb
import msgpack_numpy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import tqdm
from habitat import Config, logger
from habitat.utils.visualizations.utils import append_text_to_image
from habitat_baselines.common.base_trainer import BaseRLTrainer
from habitat_baselines.common.baseline_registry import baseline_registry
from habitat_baselines.common.environments import get_env_class
from habitat_baselines.common.tensorboard_utils import TensorboardWriter
from habitat_baselines.common.utils import batch_obs, generate_video, attention_to_image

from habitat_extensions.utils import observations_to_image
from vlnce_baselines.common.aux_losses import AuxLosses
from vlnce_baselines.common.env_utils import (
    construct_envs,
    construct_envs_auto_reset_false,
)
from vlnce_baselines.common.utils import transform_obs, init_distributed_mode
from vlnce_baselines.models.transformer_policy import TransformerPolicy


with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning)
    import tensorflow as tf

from pynvml import *
nvmlInit()
from torch.utils.data.distributed import DistributedSampler


class ObservationsDict(dict):
    def pin_memory(self):
        for k, v in self.items():
            self[k] = v.pin_memory()

        return self


def collate_fn(batch):
    """Each sample in batch: (
            obs,
            prev_actions,
            oracle_actions,
            inflec_weight,
        )
    """

    def _pad_helper(t, max_len, fill_val=0):
        pad_amount = max_len - t.size(0)
        if pad_amount == 0:
            return t
        
        pad = torch.full_like(t[0:1], fill_val).expand(pad_amount, *t.size()[1:])
        return torch.cat([t, pad], dim=0)

    transposed = list(zip(*batch))

    observations_batch = list(transposed[0])
    prev_actions_batch = list(transposed[1])
    corrected_actions_batch = list(transposed[2])
    N = len(corrected_actions_batch)
    weights_batch = list(transposed[3])
    B = len(prev_actions_batch)
    new_observations_batch = defaultdict(list)
    for sensor in observations_batch[0]:
        if sensor == 'instruction':
            for bid in range(N):
                new_observations_batch[sensor].append(observations_batch[bid][sensor])
        else: 
            for bid in range(B):
                new_observations_batch[sensor].append(observations_batch[bid][sensor])

    observations_batch = new_observations_batch

    max_traj_len = max(ele.size(0) for ele in prev_actions_batch)
#     obs_lengths=[]
    for bid in range(B):
        for sensor in observations_batch:
            if sensor == 'instruction':
                continue
            observations_batch[sensor][bid] = _pad_helper(
                observations_batch[sensor][bid], max_traj_len, fill_val=0.0
            )
#         obs_lengths.append(prev_actions_batch[bid].shape[0])
        prev_actions_batch[bid] = _pad_helper(prev_actions_batch[bid], max_traj_len)
        corrected_actions_batch[bid] = _pad_helper(
            corrected_actions_batch[bid], max_traj_len, fill_val=-1.0
        )
        weights_batch[bid] = _pad_helper(weights_batch[bid], max_traj_len)

    for sensor in observations_batch:
        observations_batch[sensor] = torch.stack(observations_batch[sensor], dim=1)
        observations_batch[sensor] = observations_batch[sensor].transpose(1,0)
        observations_batch[sensor] = observations_batch[sensor].contiguous().view(
            -1, *observations_batch[sensor].size()[2:]
        )

    prev_actions_batch = torch.stack(prev_actions_batch, dim=1)
    corrected_actions_batch = torch.stack(corrected_actions_batch, dim=1)
    weights_batch = torch.stack(weights_batch, dim=1)
    not_done_masks = torch.ones_like(corrected_actions_batch, dtype=torch.float)
    not_done_masks[0] = 0
    
    prev_actions_batch = prev_actions_batch.transpose(1,0)
    not_done_masks = not_done_masks.transpose(1,0)
    corrected_actions_batch = corrected_actions_batch.transpose(1,0)
    weights_batch = weights_batch.transpose(1,0)
    
    observations_batch = ObservationsDict(observations_batch)

    return (
        observations_batch,
        prev_actions_batch.contiguous().view(-1, 1),
        not_done_masks.contiguous().view(-1, 1),
        corrected_actions_batch,
        weights_batch,
    )


def _block_shuffle(lst, block_size):
    blocks = [lst[i : i + block_size] for i in range(0, len(lst), block_size)]
    random.shuffle(blocks)

    return [ele for block in blocks for ele in block]


class IWTrajectoryDataset(torch.utils.data.IterableDataset):
    def __init__(
        self,
        lmdb_features_dir,
        use_iw,
        inflection_weight_coef=1.0,
        lmdb_map_size=1e9,
        batch_size=1,
    ):
        super().__init__()
        self.lmdb_features_dir = lmdb_features_dir
        self.lmdb_map_size = lmdb_map_size
        self.preload_size = batch_size * 100
        self._preload = []
        self.batch_size = batch_size

        if use_iw:
            self.inflec_weights = torch.tensor([1.0, inflection_weight_coef])
        else:
            self.inflec_weights = torch.tensor([1.0, 1.0])

        with lmdb.open(
            self.lmdb_features_dir,
            map_size=int(self.lmdb_map_size),
            readonly=True,
            lock=False,
        ) as lmdb_env:
            self.length = lmdb_env.stat()["entries"]

    def _load_next(self):
        if len(self._preload) == 0:
            if len(self.load_ordering) == 0:
                raise StopIteration

            new_preload = []
            lengths = []
            with lmdb.open(
                self.lmdb_features_dir,
                map_size=int(self.lmdb_map_size),
                readonly=True,
                lock=False,
            ) as lmdb_env, lmdb_env.begin(buffers=True) as txn:
                for _ in range(self.preload_size):
                    if len(self.load_ordering) == 0:
                        break

                    new_preload.append(
                        msgpack_numpy.unpackb(
                            txn.get(str(self.load_ordering.pop()).encode()), raw=False
                        )
                    )

                    lengths.append(len(new_preload[-1][0]))

            sort_priority = list(range(len(lengths)))
            random.shuffle(sort_priority)

            sorted_ordering = list(range(len(lengths)))
            sorted_ordering.sort(key=lambda k: (lengths[k], sort_priority[k]))

            for idx in _block_shuffle(sorted_ordering, self.batch_size):
                self._preload.append(new_preload[idx])

        return self._preload.pop()

    def __next__(self):
        obs, prev_actions, oracle_actions= self._load_next()
        instruction_batch = obs['instruction'][0]
        instruction_batch = np.expand_dims(instruction_batch, axis=0)
        obs['instruction'] = instruction_batch
        for k, v in obs.items():
            obs[k] = torch.from_numpy(v)

        prev_actions = torch.from_numpy(prev_actions)
        
        
        oracle_actions = torch.from_numpy(oracle_actions)

        inflections = torch.cat(
            [
                torch.tensor([1], dtype=torch.long),
                (oracle_actions[1:] != oracle_actions[:-1]).long(),
            ]
        )
        return (obs, prev_actions, oracle_actions, self.inflec_weights[inflections])

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            start = 0
            end = self.length
        else:
            per_worker = int(np.ceil(self.length / worker_info.num_workers))

            start = per_worker * worker_info.id
            end = min(start + per_worker, self.length)

        # Reverse so we can use .pop()
        self.load_ordering = list(
            reversed(_block_shuffle(list(range(start, end)), self.preload_size))
        )

        return self


@baseline_registry.register_trainer(name="transformer_ddp")
class TransformerTrainerDDP(BaseRLTrainer):
    def __init__(self, config=None):
        super().__init__(config)
        self.actor_critic = None
        self.envs = None
        self.config.defrost()
        init_distributed_mode(self.config.DDP)
        self.config.freeze()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # self.device = (
        #     torch.device("cuda", self.config.TORCH_GPU_ID)
        #     if torch.cuda.is_available()
        #     else torch.device("cpu")
        # )
        # self.device = torch.device("cuda:{}".format(local_rank))
        self.lmdb_features_dir = self.config.DAGGER.LMDB_FEATURES_DIR.format(
            split=config.TASK_CONFIG.DATASET.SPLIT
        )
        # torch.distributed.init_process_group(backend="nccl")

    def _setup_actor_critic_agent(
        self, config: Config, load_from_ckpt: bool, ckpt_path: str
    ) -> None:
        r"""Sets up actor critic and agent.
        Args:
            config: MODEL config
        Returns:
            None
        """
        config.defrost()
        config.TORCH_GPU_ID = self.config.TORCH_GPU_ID
        config.freeze()

        if config.TRANSFORMER.use:
            self.actor_critic = TransformerPolicy(
                observation_space=self.envs.observation_spaces[0],
                action_space=self.envs.action_spaces[0],
                model_config=config,
                num_processes = self.config.NUM_PROCESSES,
                batch_size=self.config.DAGGER.BATCH_SIZE,
            )
        # if torch.cuda.device_count() > 1:
        #     self.actor_critic = nn.DataParallel(self.actor_critic)

        self.actor_critic.to(self.device)

        if self.config.DDP.distributed:
            self.actor_critic = torch.nn.parallel.DistributedDataParallel(self.actor_critic, device_ids=[self.config.DDP.gpu])
            self.actor_critic_without_ddp = self.actor_critic.module

        # self.optimizer = torch.optim.Adam(
        #     self.actor_critic.parameters(), lr=self.config.DAGGER.LR
        # )
        # self.optimizer = torch.optim.Adam(self.actor_critic.parameters(), 
        #                                  lr=self.config.MODEL.TRANSFORMER.lr, 
        #                                  betas=(0.9, 0.98), eps=1e-9)
        self.optimizer = torch.optim.AdamW(self.actor_critic_without_ddp.parameters(), 
                                         lr=self.config.MODEL.TRANSFORMER.lr, 
                                         weight_decay=self.config.MODEL.TRANSFORMER.weight_decay)

        # self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='max', factor=0.1, 
        #                                                        patience=self.config.MODEL.TRANSFORMER.scheduler_patience, 
        #                                                        verbose=True, min_lr=1e-6)

        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, 1.0, gamma=0.8)

        # self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, self.config.MODEL.TRANSFORMER.lr_drop)
        if load_from_ckpt:
            ckpt_dict = self.load_checkpoint(ckpt_path, map_location="cpu")
            self.actor_critic.load_state_dict(ckpt_dict["state_dict"])
            logger.info(f"Loaded weights from checkpoint: {ckpt_path}")
        logger.info("Finished setting up actor critic model.")

    def save_checkpoint(self, file_name) -> None:
        r"""Save checkpoint with specified name.

        Args:
            file_name: file name for checkpoint

        Returns:
            None
        """
        checkpoint = {
            "state_dict": self.actor_critic.state_dict(),
            "config": self.config,
        }
        torch.save(checkpoint, os.path.join(self.config.CHECKPOINT_FOLDER, file_name))

    def load_checkpoint(self, checkpoint_path, *args, **kwargs) -> Dict:
        r"""Load checkpoint of specified path as a dict.

        Args:
            checkpoint_path: path of target checkpoint
            *args: additional positional args
            **kwargs: additional keyword args

        Returns:
            dict containing checkpoint info
        """
        return torch.load(checkpoint_path, *args, **kwargs)

    def _update_dataset(self, data_it):
        if torch.cuda.is_available():
            with torch.cuda.device(self.device):
                torch.cuda.empty_cache()

        if self.envs is None:
            self.envs = construct_envs(self.config, get_env_class(self.config.ENV_NAME))

        prev_actions_list = []
        prev_actions = torch.zeros(
            self.config.NUM_PROCESSES, 1, device=self.device, dtype=torch.long
        )
        not_done_masks = torch.zeros(self.config.NUM_PROCESSES, 1, device=self.device)

        observations = self.envs.reset()
        observations = transform_obs(
            observations, self.config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID, is_bert=self.config.MODEL.INSTRUCTION_ENCODER.is_bert
        )
        batch = batch_obs(observations, self.device)

        episodes = [[] for _ in range(self.envs.num_envs)]
        skips = [False for _ in range(self.envs.num_envs)]
        # Populate dones with False initially
        dones = [False for _ in range(self.envs.num_envs)]

        # https://arxiv.org/pdf/1011.0686.pdf
        # Theoretically, any beta function is fine so long as it converges to
        # zero as data_it -> inf. The paper suggests starting with beta = 1 and
        # exponential decay.
        if self.config.DAGGER.P == 0.0:
            # in Python 0.0 ** 0.0 == 1.0, but we want 0.0
            beta = 0.0
        else:
            beta = self.config.DAGGER.P ** data_it

        ensure_unique_episodes = beta == 1.0

        def hook_builder(tgt_tensor):
            def hook(m, i, o):
                tgt_tensor.set_(o.cpu())

            return hook

        rgb_features = None
        rgb_hook = None
        if self.config.MODEL.RGB_ENCODER.cnn_type == "TorchVisionResNet50":
            rgb_features = torch.zeros((1,), device="cpu")
            rgb_hook = self.actor_critic.net.rgb_encoder.layer_extract.register_forward_hook(
                hook_builder(rgb_features)
            )

        depth_features = None
        depth_hook = None
        if self.config.MODEL.DEPTH_ENCODER.cnn_type == "VlnResnetDepthEncoder":
            depth_features = torch.zeros((1,), device="cpu")
            depth_hook = self.actor_critic.net.depth_encoder.visual_encoder.register_forward_hook(
                hook_builder(depth_features)
            )

        collected_eps = 0
        if ensure_unique_episodes:
            ep_ids_collected = set(
                [ep.episode_id for ep in self.envs.current_episodes()]
            )

        with tqdm.tqdm(total=self.config.DAGGER.UPDATE_SIZE) as pbar, lmdb.open(
            self.lmdb_features_dir, map_size=int(self.config.DAGGER.LMDB_MAP_SIZE)
        ) as lmdb_env, torch.no_grad():
            start_id = lmdb_env.stat()["entries"]
            txn = lmdb_env.begin(write=True)

            while collected_eps < self.config.DAGGER.UPDATE_SIZE:
                if ensure_unique_episodes:
                    envs_to_pause = []
                    current_episodes = self.envs.current_episodes()

                for i in range(self.envs.num_envs):
                    if dones[i] and not skips[i]:
                        ep = episodes[i]
                        traj_obs = batch_obs(
                            [step[0] for step in ep], device=torch.device("cpu")
                        )
                        del traj_obs["vln_oracle_action_sensor"]
                        for k, v in traj_obs.items():
                            traj_obs[k] = v.numpy()

                        transposed_ep = [
                            traj_obs,
                            np.array([step[1] for step in ep], dtype=np.int64),
                            np.array([step[2] for step in ep], dtype=np.int64),
                        ]
                        txn.put(
                            str(start_id + collected_eps).encode(),
                            msgpack_numpy.packb(transposed_ep, use_bin_type=True),
                        )

                        pbar.update()
                        collected_eps += 1

                        if (
                            collected_eps % self.config.DAGGER.LMDB_COMMIT_FREQUENCY
                        ) == 0:
                            txn.commit()
                            txn = lmdb_env.begin(write=True)

                        if ensure_unique_episodes:
                            if current_episodes[i].episode_id in ep_ids_collected:
                                envs_to_pause.append(i)
                            else:
                                ep_ids_collected.add(current_episodes[i].episode_id)

                    if dones[i]:
                        episodes[i] = []
                        prev_actions_list=[]

                if ensure_unique_episodes:
                    (
                        self.envs,
                        prev_actions_list,
                        not_done_masks,
                        prev_actions,
                        batch,
                    ) = self._pause_envs(
                        envs_to_pause,
                        self.envs,
                        prev_actions_list,
                        not_done_masks,
                        prev_actions,
                        batch,
                    )
                    if self.envs.num_envs == 0:
                        break

                (_, actions, _, prev_actions_list) = self.actor_critic.act(
                    batch,
                    prev_actions_list,
                    prev_actions,
                    not_done_masks,
                    deterministic=False,
                )
                actions = torch.where(
                    torch.rand_like(actions, dtype=torch.float) < beta,
                    batch["vln_oracle_action_sensor"].long(),
                    actions,
                )

                for i in range(self.envs.num_envs):
                    if rgb_features is not None:
                        observations[i]["rgb_features"] = rgb_features[i]
                        del observations[i]["rgb"]

                    if depth_features is not None:
                        observations[i]["depth_features"] = depth_features[i]
                        del observations[i]["depth"]

                    episodes[i].append(
                        (
                            observations[i],
                            prev_actions[i].item(),
                            batch["vln_oracle_action_sensor"][i].item(),
                        )
                    )

                skips = batch["vln_oracle_action_sensor"].long() == -1
                actions = torch.where(skips, torch.zeros_like(actions), actions)
                skips = skips.squeeze(-1).to(device="cpu", non_blocking=True)

                prev_actions.copy_(actions)

                outputs = self.envs.step([a[0].item() for a in actions])
                observations, rewards, dones, _ = [list(x) for x in zip(*outputs)]

                not_done_masks = torch.tensor(
                    [[0.0] if done else [1.0] for done in dones],
                    dtype=torch.float,
                    device=self.device,
                )

                observations = transform_obs(
                    observations, self.config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID, is_bert=self.config.MODEL.INSTRUCTION_ENCODER.is_bert
                )
                batch = batch_obs(observations, self.device)

            txn.commit()

        self.envs.close()
        self.envs = None

        if rgb_hook is not None:
            rgb_hook.remove()
        if depth_hook is not None:
            depth_hook.remove()

    def _update_agent(
        self, observations, prev_actions, not_done_masks, corrected_actions, weights
    ):
        T, N = corrected_actions.size()
        self.optimizer.zero_grad()

        criterion = nn.CrossEntropyLoss(ignore_index=-1, reduction="none")
        prev_actions_list =None
        AuxLosses.clear()

        distribution = self.actor_critic.build_distribution(
            observations, prev_actions_list, prev_actions, not_done_masks
        )

        logits = distribution.logits
        # logits = logits.view(T, N, -1)
        corrected_actions = corrected_actions.contiguous().view(T*N)

        # action_loss = F.cross_entropy(
        #     logits.permute(0, 2, 1), corrected_actions, reduction="none"
        # )
        # action_loss = F.cross_entropy(
        #     logits, corrected_actions, reduction="none"
        # )
        action_loss = criterion(logits, corrected_actions)
        action_loss = action_loss.view(T, N)
        action_loss = ((weights * action_loss).sum(0) / weights.sum(0)).mean()
        # action_loss = action_loss.mean()

        aux_mask = (weights > 0).view(-1)
        aux_loss = AuxLosses.reduce(aux_mask)

        loss = action_loss + aux_loss
        loss.backward()
        self.optimizer.step()

        # for obj in gc.get_objects():
        #     try:
        #         if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                    
        #             print(type(obj), obj.size())
        #     except:
        #         pass

        loss_data = loss.item()
        action_loss_data = loss.item()

        if isinstance(aux_loss, torch.Tensor):
            aux_loss_data = aux_loss.item()
        else:
            aux_loss_data = aux_loss
        del distribution, logits, loss, action_loss, aux_loss, aux_mask

        return loss_data, action_loss_data, aux_loss_data

    def train(self) -> None:
        r"""Main method for training DAgger.

        Returns:
            None
        """
        os.makedirs(self.lmdb_features_dir, exist_ok=True)
        os.makedirs(self.config.CHECKPOINT_FOLDER, exist_ok=True)

        if self.config.DAGGER.PRELOAD_LMDB_FEATURES:
            try:
                lmdb.open(self.lmdb_features_dir, readonly=True)
            except lmdb.Error as err:
                logger.error("Cannot open database for teacher forcing preload.")
                raise err
        else:
            with lmdb.open(
                self.lmdb_features_dir, map_size=int(self.config.DAGGER.LMDB_MAP_SIZE)
            ) as lmdb_env, lmdb_env.begin(write=True) as txn:
                txn.drop(lmdb_env.open_db())

        split = self.config.TASK_CONFIG.DATASET.SPLIT
        self.config.defrost()
        self.config.TASK_CONFIG.TASK.NDTW.SPLIT = split
        self.config.TASK_CONFIG.TASK.SDTW.SPLIT = split

        # if doing teacher forcing, don't switch the scene until it is complete
        if self.config.DAGGER.P == 1.0:
            self.config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_STEPS = (
                -1
            )
        self.config.freeze()

        if self.config.DAGGER.PRELOAD_LMDB_FEATURES:
            # when preloadeding features, its quicker to just load one env as we just
            # need the observation space from it.
            single_proc_config = self.config.clone()
            single_proc_config.defrost()
            single_proc_config.NUM_PROCESSES = 1
            single_proc_config.freeze()
            self.envs = construct_envs(
                single_proc_config, get_env_class(self.config.ENV_NAME)
            )
        else:
            self.envs = construct_envs(self.config, get_env_class(self.config.ENV_NAME))

        self._setup_actor_critic_agent(
            self.config.MODEL,
            self.config.DAGGER.LOAD_FROM_CKPT,
            self.config.DAGGER.CKPT_TO_LOAD,
        )

        logger.info(
            "agent number of parameters: {}".format(
                sum(param.numel() for param in self.actor_critic.parameters())
            )
        )
        logger.info(
            "agent number of trainable parameters: {}".format(
                sum(
                    p.numel() for p in self.actor_critic.parameters() if p.requires_grad
                )
            )
        )

        if self.config.DAGGER.PRELOAD_LMDB_FEATURES:
            self.envs.close()
            del self.envs
            self.envs = None

        with TensorboardWriter(
            self.config.TENSORBOARD_DIR, flush_secs=self.flush_secs, purge_step=0
        ) as writer:
            for dagger_it in range(self.config.DAGGER.ITERATIONS):
                step_id = 0
                if not self.config.DAGGER.PRELOAD_LMDB_FEATURES:
                    self._update_dataset(
                        dagger_it + (1 if self.config.DAGGER.LOAD_FROM_CKPT else 0)
                    )

                if torch.cuda.is_available():
                    with torch.cuda.device(self.device):
                        torch.cuda.empty_cache()
                gc.collect()

                dataset = IWTrajectoryDataset(
                    self.lmdb_features_dir,
                    self.config.DAGGER.USE_IW,
                    inflection_weight_coef=self.config.MODEL.inflection_weight_coef,
                    lmdb_map_size=self.config.DAGGER.LMDB_MAP_SIZE,
                    batch_size=self.config.DAGGER.BATCH_SIZE,
                )
                # if self.config.DDP.distributed:
                #     sampler = DistributedSampler(dataset, shuffle=False)
                diter = torch.utils.data.DataLoader(
                    dataset,
                    batch_size=self.config.DAGGER.BATCH_SIZE,
                    shuffle=False,
                    collate_fn=collate_fn,
                    pin_memory=False,
                    drop_last=True,  # drop last batch if smaller
                    num_workers=3,
                )

                AuxLosses.activate()
                print("starting training loop")
                for epoch in tqdm.trange(self.config.DAGGER.EPOCHS):
                    # if self.config.DDP.distributed:
                    #     sampler.set_epoch(epoch)
                    for batch in tqdm.tqdm(
                        diter, total=dataset.length // dataset.batch_size, leave=False
                    ):
                        (
                            observations_batch,
                            prev_actions_batch,
                            not_done_masks,
                            corrected_actions_batch,
                            weights_batch,
                        ) = batch

                        if 'semantic' in observations_batch:
                            del observations_batch['semantic']
                        # if 'instruction' in observations_batch:
                        #     del observations_batch['instruction']
                        observations_batch = {
                            k: v.to(device=self.device, non_blocking=True)
                            for k, v in observations_batch.items()
                        }
                        try:
                            loss, action_loss, aux_loss = self._update_agent(
                                observations_batch,
                                prev_actions_batch.to(
                                    device=self.device, non_blocking=True
                                ),
                                not_done_masks.to(
                                    device=self.device, non_blocking=True
                                ),
                                corrected_actions_batch.to(
                                    device=self.device, non_blocking=True
                                ),
                                weights_batch.to(device=self.device, non_blocking=True),
                            )
                        except:
                            logger.info(
                                "ERROR: failed to update agent. Updating agent with batch size of 1."
                            )
                            loss, action_loss, aux_loss = 0, 0, 0
                            prev_actions_batch = prev_actions_batch.cpu()
                            not_done_masks = not_done_masks.cpu()
                            corrected_actions_batch = corrected_actions_batch.cpu()
                            weights_batch = weights_batch.cpu()
                            observations_batch = {
                                k: v.cpu() for k, v in observations_batch.items()
                            }

                            for i in range(not_done_masks.size(0)):
                                output = self._update_agent(
                                    {
                                        k: v[i].to(
                                            device=self.device, non_blocking=True
                                        )
                                        for k, v in observations_batch.items()
                                    },
                                    prev_actions_batch[i].to(
                                        device=self.device, non_blocking=True
                                    ),
                                    not_done_masks[i].to(
                                        device=self.device, non_blocking=True
                                    ),
                                    corrected_actions_batch[i].to(
                                        device=self.device, non_blocking=True
                                    ),
                                    weights_batch[i].to(
                                        device=self.device, non_blocking=True
                                    ),
                                )
                                loss += output[0]
                                action_loss += output[1]
                                aux_loss += output[2]

                        logger.info(f"train_loss: {loss}")
                        logger.info(f"train_action_loss: {action_loss}")
                        logger.info(f"train_aux_loss: {aux_loss}")
                        logger.info(f"Batches processed: {step_id}.")
                        logger.info(f"On DAgger iter {dagger_it}, Epoch {epoch}.")
                        writer.add_scalar(f"train_loss_iter_{dagger_it}", loss, step_id)
                        writer.add_scalar(
                            f"train_action_loss_iter_{dagger_it}", action_loss, step_id
                        )

                        h1 = nvmlDeviceGetHandleByIndex(0)
                        info1 = nvmlDeviceGetMemoryInfo(h1)

                        h2 = nvmlDeviceGetHandleByIndex(1)
                        info2 = nvmlDeviceGetMemoryInfo(h2)

                        h3 = nvmlDeviceGetHandleByIndex(2)
                        info3 = nvmlDeviceGetMemoryInfo(h3)
                        writer.add_scalar(
                            f"train_aux_loss_iter_{dagger_it}", aux_loss, step_id
                        )
                        writer.add_scalar(
                            f"GPU_0_mem_usage_{dagger_it}", info1.used, step_id
                        )
                        writer.add_scalar(
                            f"GPU_1_mem_usage_{dagger_it}", info2.used, step_id
                        )
                        writer.add_scalar(
                            f"GPU_2_mem_usage_{dagger_it}", info3.used, step_id
                        )
                        writer.add_scalar(
                            f"Cache_0_mem_usage_{dagger_it}", torch.cuda.memory_cached(0), step_id
                        )
                        writer.add_scalar(
                            f"Cache_1_mem_usage_{dagger_it}", torch.cuda.memory_cached(1), step_id
                        )
                        writer.add_scalar(
                            f"Cache_2_mem_usage_{dagger_it}", torch.cuda.memory_cached(2), step_id
                        )
                        del loss
                        # torch.cuda.empty_cache() 
                        step_id += 1
                    self.scheduler.step()
                    self.save_checkpoint(
                        f"ckpt.{dagger_it * self.config.DAGGER.EPOCHS + epoch}.pth"
                    )
                AuxLosses.deactivate()

    @staticmethod
    def _pause_envs(
        envs_to_pause,
        envs,
        prev_actions_list,
        not_done_masks,
        prev_actions,
        batch,
    ):
        # pausing self.envs with no new episode
        if len(envs_to_pause) > 0:
            state_index = list(range(envs.num_envs))
            for idx in reversed(envs_to_pause):
                state_index.pop(idx)
                envs.pause_at(idx)

            # indexing along the batch dimensions
            not_done_masks = not_done_masks[state_index]
            prev_actions = prev_actions[state_index]

            for k, v in batch.items():
                batch[k] = v[state_index]

        return (envs, prev_actions_list, not_done_masks, prev_actions, batch)

    def _eval_checkpoint(
        self, checkpoint_path: str, writer: TensorboardWriter, checkpoint_index: int = 0
    ) -> None:
        r"""Evaluates a single checkpoint. Assumes episode IDs are unique.

        Args:
            checkpoint_path: path of checkpoint
            writer: tensorboard writer object for logging to tensorboard
            checkpoint_index: index of cur checkpoint for logging

        Returns:
            None
        """
        logger.info(f"checkpoint_path: {checkpoint_path}")

        if self.config.EVAL.USE_CKPT_CONFIG:
            config = self._setup_eval_config(
                self.load_checkpoint(checkpoint_path, map_location="cpu")["config"]
            )
        else:
            config = self.config.clone()

        config.defrost()
        config.TASK_CONFIG.DATASET.SPLIT = config.EVAL.SPLIT
        config.TASK_CONFIG.TASK.NDTW.SPLIT = config.EVAL.SPLIT
        config.TASK_CONFIG.TASK.SDTW.SPLIT = config.EVAL.SPLIT
        config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.SHUFFLE = False
        config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_STEPS = -1
        if len(config.VIDEO_OPTION) > 0:
            config.defrost()
            config.TASK_CONFIG.TASK.MEASUREMENTS.append("TOP_DOWN_MAP")
            config.TASK_CONFIG.TASK.MEASUREMENTS.append("COLLISIONS")

        config.freeze()

        # setup agent
        self.envs = construct_envs_auto_reset_false(
            config, get_env_class(config.ENV_NAME)
        )
        self.device = (
            torch.device("cuda", config.TORCH_GPU_ID)
            if torch.cuda.is_available()
            else torch.device("cpu")
        )

        self._setup_actor_critic_agent(config.MODEL, True, checkpoint_path)

        observations = self.envs.reset()
        observations = transform_obs(
            observations, config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID, is_bert=self.config.MODEL.INSTRUCTION_ENCODER.is_bert
        )
        batch = batch_obs(observations, self.device)
        # batch["instruction_batch"] = batch['instruction']
        # del batch['instruction']

        eval_recurrent_hidden_states = []
        prev_actions = torch.zeros(
            config.NUM_PROCESSES, 1, device=self.device, dtype=torch.long
        )
        not_done_masks = torch.zeros(config.NUM_PROCESSES, 1, device=self.device)

        stats_episodes = {}  # dict of dicts that stores stats per episode

        if len(config.VIDEO_OPTION) > 0:
            os.makedirs(config.VIDEO_DIR, exist_ok=True)
            rgb_frames = [[] for _ in range(config.NUM_PROCESSES)]

        if config.PLOT_ATTENTION:
            attention_weights = [[] for _ in range(config.NUM_PROCESSES)]
            save_actions = [[] for _ in range(config.NUM_PROCESSES)]

        self.actor_critic.eval()
        k=0
        min_2nd_dim = 1000
        while (
            self.envs.num_envs > 0 and len(stats_episodes) < config.EVAL.EPISODE_COUNT
        ):
            current_episodes = self.envs.current_episodes()

            # print(eval_recurrent_hidden_states)
            # print("\n")

            with torch.no_grad():
                (_, actions, _, eval_recurrent_hidden_states) = self.actor_critic.act(
                    batch,
                    eval_recurrent_hidden_states,
                    prev_actions,
                    not_done_masks,
                    deterministic=True,
                )
                prev_actions.copy_(actions)

            outputs = self.envs.step([a[0].item() for a in actions])
            observations, _, dones, infos = [list(x) for x in zip(*outputs)]

            # print("dones",dones, "\n")
            # print("not_done_masks", not_done_masks)

            not_done_masks = torch.tensor(
                [[0.0] if done else [1.0] for done in dones],
                dtype=torch.float,
                device=self.device,
            )
            # reset envs and observations if necessary
            for i in range(self.envs.num_envs):
                # print("-----------------------------")
                # print("Current episodes", self.envs.current_episodes()[i].episode_id)
                # print("loop number:",k)
                # print("-----------------------------")
                if config.PLOT_ATTENTION:
                    attention_weights[i].append(batch['lang_attention'][i])
                    min_dim = batch['lang_attention'][i].shape[1]
                    min_2nd_dim = np.minimum(min_2nd_dim,min_dim)
                    save_actions[i].append(actions[i])

                    # print("-----------------------------------")
                    # print("Env: ", i, " Attn shape: ", batch['lang_attention'][i].shape)
                    # print("-----------------------------------")
                if len(config.VIDEO_OPTION) > 0:
                    # print(observations[i])
                    # print("----------------------------------")
                    frame = observations_to_image(observations[i], infos[i])
                    frame = append_text_to_image(
                        frame, current_episodes[i].instruction.instruction_text
                    )
                    rgb_frames[i].append(frame)

                if not dones[i]:
                    continue
                eval_recurrent_hidden_states = []
                stats_episodes[current_episodes[i].episode_id] = infos[i]
                
                # print("Current episode ID:", current_episodes[i].episode_id, "\n")

                # print(stats_episodes[current_episodes[i].episode_id],"\n")
                # print("---------------------------------------------")
                observations[i] = self.envs.reset_at(i)[0]
                prev_actions[i] = torch.zeros(1, dtype=torch.long)

                isspl_not_zero = stats_episodes[current_episodes[i].episode_id]["spl"] != 0.0

                metrics={"SPL":round(
                            stats_episodes[current_episodes[i].episode_id]["spl"], 6
                        ) }  
                if len(config.VIDEO_OPTION) > 0:
                    # if stats_episodes[current_episodes[i].episode_id]["spl"] == 0.0:
                    #     del stats_episodes[current_episodes[i].episode_id]["top_down_map"]
                    #     del stats_episodes[current_episodes[i].episode_id]["collisions"]
                    #     rgb_frames[i] = []
                    #     continue
                    generate_video(
                        video_option=config.VIDEO_OPTION,
                        video_dir=config.VIDEO_DIR,
                        images=rgb_frames[i],
                        episode_id=current_episodes[i].episode_id,
                        checkpoint_idx=checkpoint_index,
                        metrics=metrics,
                        tb_writer=writer,
                    )
                    del stats_episodes[current_episodes[i].episode_id]["top_down_map"]
                    del stats_episodes[current_episodes[i].episode_id]["collisions"]
                    rgb_frames[i] = []
                del stats_episodes[current_episodes[i].episode_id]["top_down_map"]
                if config.PLOT_ATTENTION:
                    # if stats_episodes[current_episodes[i].episode_id]["spl"] == 0.0:
                        # attention_weights[i] = [] 
                        # save_actions[i] =[]
                    #     continue
                    for j in range(len(attention_weights[i])):
                        attention_weights[i][j] = attention_weights[i][j][:,:min_2nd_dim]
                    #     print(attention_weights[i][j].shape)
                    # print("--------------------------------------------------------------")
                    # print("Min Dim",min_2nd_dim)
                    # attention_weights[i] = attention_weights[i][4:]
                    attention_weights[i]= torch.cat(attention_weights[i], dim=0).cpu().numpy()
                    # print(attention_weights[i].shape)
                    attention_to_image(
                            image_dir = config.VIDEO_DIR,
                            attention = attention_weights[i],
                            episode_id=current_episodes[i].episode_id,
                            checkpoint_idx=checkpoint_index,
                            metrics=metrics,
                            actions = save_actions[i]
                        )
                    attention_weights[i] = [] 
                    save_actions[i] =[]
                    max_2nd_dim = 1000
                    # for i in range(len(attention_weights)):
                    #     attention_weights[i]= torch.cat(attention_weights[i], dim=0).cpu().numpy()
                    #     print(attention_weights[i].shape)
                    #     attention_to_image(
                    #         image_dir = config.VIDEO_DIR,
                    #         attention = attention_weights[i],
                    #         episode_id=current_episodes[i].episode_id,
                    #         checkpoint_idx=checkpoint_index,
                    #         metrics=metrics,
                    #         actions = save_actions[i]
                    #     )
                else:                       
                    del stats_episodes[current_episodes[i].episode_id]["top_down_map"]
            observations = transform_obs(
                observations, config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID, is_bert=self.config.MODEL.INSTRUCTION_ENCODER.is_bert
            )
            batch = batch_obs(observations, self.device)
            # batch["instruction_batch"] = batch['instruction']
            # del batch['instruction']

            envs_to_pause = []
            next_episodes = self.envs.current_episodes()

            for i in range(self.envs.num_envs):
                if next_episodes[i].episode_id in stats_episodes:
                    envs_to_pause.append(i)

            (
                self.envs,
                eval_recurrent_hidden_states,
                not_done_masks,
                prev_actions,
                batch,
            ) = self._pause_envs(
                envs_to_pause,
                self.envs,
                eval_recurrent_hidden_states,
                not_done_masks,
                prev_actions,
                batch,
            )

        self.envs.close()

        aggregated_stats = {}
        num_episodes = len(stats_episodes)
        # print("-----------------------------------------------")
        # print(stats_episodes.values())
        for stat_key in next(iter(stats_episodes.values())).keys():
            # for v in stats_episodes.values():
            #     print (stat_key, "-------", v[stat_key])
            aggregated_stats[stat_key] = (
                sum([v[stat_key] for v in stats_episodes.values()]) / num_episodes
            )

        split = config.TASK_CONFIG.DATASET.SPLIT
        os.makedirs(config.EVAL.VAL_LOG_DIR, exist_ok=True)
        val_log_path = os.path.join(config.EVAL.VAL_LOG_DIR,f"stats_ckpt_{checkpoint_index}_{split}.json")
        with open(val_log_path, "w") as f:
            json.dump(aggregated_stats, f, indent=4)

        logger.info(f"Episodes evaluated: {num_episodes}")
        checkpoint_num = checkpoint_index + 1
        for k, v in aggregated_stats.items():
            logger.info(f"Average episode {k}: {v:.6f}")
            writer.add_scalar(f"eval_{split}_{k}", v, checkpoint_num)
