import torch
import numpy as np
import torch.nn as nn
import gym
import os
from collections import deque
import random


class eval_mode(object):
    def __init__(self, *models):
        self.models = models

    def __enter__(self):
        self.prev_states = []
        for model in self.models:
            self.prev_states.append(model.training)
            model.train(False)

    def __exit__(self, *args):
        for model, state in zip(self.models, self.prev_states):
            model.train(state)
        return False


def soft_update_params(net, target_net, tau):
    for param, target_param in zip(net.parameters(), target_net.parameters()):
        target_param.data.copy_(
            tau * param.data + (1 - tau) * target_param.data
        )


def set_seed_everywhere(seed):
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)


def module_hash(module):
    result = 0
    for tensor in module.state_dict().values():
        result += tensor.sum().item()
    return result


def make_dir(dir_path):
    try:
        os.mkdir(dir_path)
    except OSError:
        pass
    return dir_path


def preprocess_obs(obs, bits=5):
    """Preprocessing image, see https://arxiv.org/abs/1807.03039."""
    bins = 2**bits
    assert obs.dtype == torch.float32
    if bits < 8:
        obs = torch.floor(obs / 2**(8 - bits))
    obs = obs / bins
    obs = obs + torch.rand_like(obs) / bins
    obs = obs - 0.5
    return obs


class ReplayBuffer(object):
    """Buffer to store environment transitions."""
    def __init__(self, obs_space, act_space, capacity, batch_size, device):
        self.obs_space = obs_space
        self.act_space = act_space
        self.capacity = capacity
        self.batch_size = batch_size
        self.device = device

        self.cam = np.empty((capacity, *obs_space['camera']), dtype=np.uint8)
        self.proprio = np.empty((capacity, *obs_space['proprioception']), dtype=np.float32) if "proprioception" in obs_space else None
        self.next_cam = np.empty((capacity, *obs_space['camera']), dtype=np.uint8)
        self.next_proprio = np.empty((capacity, *obs_space['proprioception']), dtype=np.float32) if "proprioception" in obs_space else None
        self.actions = np.empty((capacity, *act_space.shape), dtype=np.float32)
        self.rewards = np.empty((capacity, 1), dtype=np.float32)
        self.not_dones = np.empty((capacity, 1), dtype=np.float32)

        self.idx = 0
        self.last_save = 0
        self.full = False

    def add(self, obs, action, reward, next_obs, done):
        np.copyto(self.cam[self.idx], obs[0])
        if "proprioception" in self.obs_space: np.copyto(self.proprio[self.idx], obs[1])
        np.copyto(self.actions[self.idx], action)
        np.copyto(self.rewards[self.idx], reward)
        np.copyto(self.next_cam[self.idx], next_obs[0])
        if "proprioception" in self.obs_space: np.copyto(self.next_proprio[self.idx], next_obs[1])
        np.copyto(self.not_dones[self.idx], not done)

        self.idx = (self.idx + 1) % self.capacity
        self.full = self.full or self.idx == 0

    def sample(self):
        idxs = np.random.randint(
            0, self.capacity if self.full else self.idx, size=self.batch_size
        )
        if "proprioception" in self.obs_space:
            obses = [torch.as_tensor(self.cam[idxs], device=self.device).float(), torch.as_tensor(self.proprio[idxs], device=self.device).float()]
            actions = torch.as_tensor(self.actions[idxs], device=self.device)
            rewards = torch.as_tensor(self.rewards[idxs], device=self.device)
            next_obses = [torch.as_tensor(self.next_cam[idxs], device=self.device).float(), torch.as_tensor(self.next_proprio[idxs], device=self.device).float()]
            not_dones = torch.as_tensor(self.not_dones[idxs], device=self.device) 
        else:
            obses = torch.as_tensor(self.cam[idxs], device=self.device).float()
            actions = torch.as_tensor(self.actions[idxs], device=self.device)
            rewards = torch.as_tensor(self.rewards[idxs], device=self.device)
            next_obses = torch.as_tensor(self.next_cam[idxs], device=self.device).float()
            not_dones = torch.as_tensor(self.not_dones[idxs], device=self.device)

        return obses, actions, rewards, next_obses, not_dones 

    def save(self, save_dir):
        if self.idx == self.last_save:
            return
        path = os.path.join(save_dir, '%d_%d.pt' % (self.last_save, self.idx))
        payload = [
            self.obses[self.last_save:self.idx],
            self.next_obses[self.last_save:self.idx],
            self.actions[self.last_save:self.idx],
            self.rewards[self.last_save:self.idx],
            self.not_dones[self.last_save:self.idx]
        ]
        self.last_save = self.idx
        torch.save(payload, path)

    def load(self, save_dir):
        chunks = os.listdir(save_dir)
        chucks = sorted(chunks, key=lambda x: int(x.split('_')[0]))
        for chunk in chucks:
            start, end = [int(x) for x in chunk.split('.')[0].split('_')]
            path = os.path.join(save_dir, chunk)
            payload = torch.load(path)
            assert self.idx == start
            self.obses[start:end] = payload[0]
            self.next_obses[start:end] = payload[1]
            self.actions[start:end] = payload[2]
            self.rewards[start:end] = payload[3]
            self.not_dones[start:end] = payload[4]
            self.idx = end


class FrameStack():
    def __init__(self, env, k):
        self.env = env
        self._k = k
        self._frames = deque([], maxlen=k)
        self._robot_state = None
        self.observation_space ={
            "proprioception": env.observation_space["proprioception"],
            "camera": (k*env.observation_space["camera"][0],env.observation_space["camera"][1],env.observation_space["camera"][2])
        }
        self.action_space = env.action_space
        self._max_episode_steps = 300
        self.reset()

    def reset(self):
        obs = self.env.reset()
        if self.env.observation_type['q'] or self.env.observation_type['x']: self._robot_state = obs[1]
        for _ in range(self._k):
            self._frames.append(obs[0])
        return self._get_obs()

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        self._frames.append(obs[0])
        if self.env.observation_type['q'] or self.env.observation_type['x']: self._robot_state = obs[1]
        return self._get_obs(), reward, done, info

    def _get_obs(self):
        assert len(self._frames) == self._k
        obs = ()
        if self.env.observation_type['camera']: obs = obs + (np.concatenate(list(self._frames), axis=0),)
        if self.env.observation_type['q'] or self.env.observation_type['x']: obs = obs + (self._robot_state,)
        return obs