import numpy as np
import torch
import argparse
import os
import math
import gym
import sys
import random
import time
import json
import copy

import utils
from logger import Logger
from video import VideoRecorder

from sac_ae import SacAeAgent

from sai2_environment.robot_env import RobotEnv
from sai2_environment.action_space import ActionSpace

param_set = 'test' # should be mini or default or test
save = True # True or false, if FTrue save model etc., else save nothing

def parse_args():
    parser = argparse.ArgumentParser()
     # environment
    parser.add_argument('--domain_name', default='panda')
    parser.add_argument('--task_name', default='peg_in_hole')
    parser.add_argument('--image_size', default=128, type=int)
    parser.add_argument('--action_repeat', default=1, type=int)
    parser.add_argument('--frame_stack', default=3, type=int)
    parser.add_argument('--agent', default='sac_ae', type=str)
    # critic
    parser.add_argument('--critic_lr', default=1e-3, type=float)
    parser.add_argument('--critic_beta', default=0.9, type=float)
    parser.add_argument('--critic_tau', default=0.01, type=float)
    # actor
    parser.add_argument('--actor_lr', default=1e-3, type=float)
    parser.add_argument('--actor_beta', default=0.9, type=float)
    parser.add_argument('--actor_log_std_min', default=-10, type=float)
    parser.add_argument('--actor_log_std_max', default=2, type=float)
    # encoder/decoder
    parser.add_argument('--encoder_type', default='pixel', type=str)
    parser.add_argument('--encoder_feature_dim', default=122, type=int)
    parser.add_argument('--encoder_lr', default=1e-3, type=float)
    parser.add_argument('--encoder_tau', default=0.05, type=float)
    parser.add_argument('--alpha_beta', default=0.5, type=float)
    parser.add_argument('--decoder_type', default='pixel', type=str)
    # misc
    parser.add_argument('--seed', default=1, type=int)
    parser.add_argument('--work_dir', default='.', type=str)
    parser.add_argument('--decoder_lr', default=1e-3, type=float)
    parser.add_argument('--decoder_update_freq', default=1, type=int)
    parser.add_argument('--decoder_latent_lambda', default=1e-6, type=float)
    parser.add_argument('--decoder_weight_lambda', default=1e-7, type=float)
    parser.add_argument('--num_layers', default=4, type=int)
    parser.add_argument('--num_filters', default=32, type=int)
    # sac
    parser.add_argument('--discount', default=0.99, type=float)
    parser.add_argument('--init_temperature', default=0.1, type=float)
    parser.add_argument('--alpha_lr', default=1e-4, type=float)
    # save
    parser.add_argument('--save_tb', default=True, action='store_true')
    parser.add_argument('--save_model', default=save, action='store_true')
    parser.add_argument('--save_buffer', default=save, action='store_true')
    parser.add_argument('--save_video', default=save, action='store_true')


    if param_set == 'default':
        # replay buffer
        parser.add_argument('--replay_buffer_capacity', default=100000, type=int) # Changed from 1e7 to 1e6
        # train
        parser.add_argument('--init_steps', default=1000, type=int) # Was 1000 before
        parser.add_argument('--num_train_steps', default=100000, type=int)
        parser.add_argument('--batch_size', default=128, type=int)
        parser.add_argument('--hidden_dim', default=1024, type=int)
        # eval
        parser.add_argument('--eval_freq', default=10000, type=int)
        parser.add_argument('--num_eval_episodes', default=10, type=int)
        # critic
        parser.add_argument('--critic_target_update_freq', default=2, type=int)
        # actor
        parser.add_argument('--actor_update_freq', default=2, type=int)

    elif param_set == 'mini':
        # replay buffer
        parser.add_argument('--replay_buffer_capacity', default=100000, type=int) # Changed from 1e7 to 1e6
        # train
        parser.add_argument('--init_steps', default=20, type=int) # Was 1000 before
        parser.add_argument('--num_train_steps', default=100000, type=int)
        parser.add_argument('--batch_size', default=128, type=int)
        parser.add_argument('--hidden_dim', default=1024, type=int)
        # eval
        parser.add_argument('--eval_freq', default=1000, type=int)
        parser.add_argument('--num_eval_episodes', default=5, type=int)
        # critic
        parser.add_argument('--critic_target_update_freq', default=2, type=int)
        # actor
        parser.add_argument('--actor_update_freq', default=2, type=int)

    elif param_set == 'test':
        # replay buffer
        parser.add_argument('--replay_buffer_capacity', default=100000, type=int) # Changed from 1e7 to 1e6
        # train
        parser.add_argument('--init_steps', default=3000, type=int) # Was 1000 before
        parser.add_argument('--num_train_steps', default=100000, type=int)
        parser.add_argument('--batch_size', default=128, type=int)
        parser.add_argument('--hidden_dim', default=512, type=int)
        # eval
        parser.add_argument('--eval_freq', default=1000, type=int)
        parser.add_argument('--num_eval_episodes', default=5, type=int)
        # critic
        parser.add_argument('--critic_target_update_freq', default=2, type=int)
        # actor
        parser.add_argument('--actor_update_freq', default=2, type=int)

    args = parser.parse_args()
    return args


def evaluate(env, agent, video, num_episodes, L, step):
    for i in range(num_episodes):
        a_anti_stuck = np.array([0,0,0.1,0,0,0])
        env.step(a_anti_stuck)
        obs = env.reset()
        video.init(enabled=(i == 0))
        done = False
        episode_reward = 0
        step_counter = 0
        while not done:
            with utils.eval_mode(agent):
                action = agent.select_action(obs)
                action = np.multiply(action,env.action_space.high)
            obs, reward, done, _ = env.step(action)
            print("TE: {}   | TS: {}   | TR: {:.4f} | TER: {:.4f} | TA: {}".format(i, step_counter, round(reward,4), round(episode_reward,4), action))
            step_counter += 1
            video.record(env)
            episode_reward += reward
        video.save('%d.mp4' % step)
        L.log('eval/episode_reward', episode_reward, step)
    L.dump(step)


def make_agent(obs_shape, action_shape, args, device):
    if args.agent == 'sac_ae':
        return SacAeAgent(
            obs_shape=obs_shape,
            action_shape=action_shape,
            device=device,
            hidden_dim=args.hidden_dim,
            discount=args.discount,
            init_temperature=args.init_temperature,
            alpha_lr=args.alpha_lr,
            alpha_beta=args.alpha_beta,
            actor_lr=args.actor_lr,
            actor_beta=args.actor_beta,
            actor_log_std_min=args.actor_log_std_min,
            actor_log_std_max=args.actor_log_std_max,
            actor_update_freq=args.actor_update_freq,
            critic_lr=args.critic_lr,
            critic_beta=args.critic_beta,
            critic_tau=args.critic_tau,
            critic_target_update_freq=args.critic_target_update_freq,
            encoder_type=args.encoder_type,
            encoder_feature_dim=args.encoder_feature_dim,
            encoder_lr=args.encoder_lr,
            encoder_tau=args.encoder_tau,
            decoder_type=args.decoder_type,
            decoder_lr=args.decoder_lr,
            decoder_update_freq=args.decoder_update_freq,
            decoder_latent_lambda=args.decoder_latent_lambda,
            decoder_weight_lambda=args.decoder_weight_lambda,
            num_layers=args.num_layers,
            num_filters=args.num_filters
        )
    else:
        assert 'agent is not supported: %s' % args.agent


def main():
    args = parse_args()
    utils.set_seed_everywhere(args.seed)

    # Robot stuff
    action_space = ActionSpace.DELTA_EE_POSE_IMPEDANCE 
    blocking_action = True
    env = RobotEnv(name='peg_in_hole',
                   simulation=True,
                   action_space=action_space,
                   isotropic_gains=True,
                   render=False,
                   blocking_action=blocking_action,
                   rotation_axis=(0, 0, 1),
                   observation_type=dict(camera=1, q=0, dq=0, tau=0, x=0, dx=0))    


    # stack several consecutive frames together
    if args.encoder_type == 'pixel':
        env = utils.FrameStack(env, k=args.frame_stack)

    utils.make_dir(args.work_dir)
    video_dir = utils.make_dir(os.path.join(args.work_dir, 'video'))
    model_dir = utils.make_dir(os.path.join(args.work_dir, 'model'))
    buffer_dir = utils.make_dir(os.path.join(args.work_dir, 'buffer'))

    video = VideoRecorder(video_dir if args.save_video else None)

    with open(os.path.join(args.work_dir, 'args.json'), 'w') as f:
        json.dump(vars(args), f, sort_keys=True, indent=4)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # the dmc2gym wrapper standardizes actions
    #assert env.action_space.low.min()   >= -1
    #assert env.action_space.high.max()  <=  1

    replay_buffer = utils.ReplayBuffer(
        obs_shape=env.observation_space['camera'],
        action_shape=env.action_space.shape,
        capacity=args.replay_buffer_capacity,
        batch_size=args.batch_size,
        device=device
    )

    agent = make_agent(
        obs_shape=env.observation_space['camera'],
        action_shape=env.action_space.shape,
        args=args,
        device=device
    )

    L = Logger(args.work_dir, use_tb=args.save_tb)

    episode, episode_reward, prev_episode_reward, done = 0, 0, 0, True
    start_time = time.time()
    for step in range(args.num_train_steps):
        if done:
            if step > 0:
                L.log('train/duration', time.time() - start_time, step)
                start_time = time.time()
                L.dump(step)

            # evaluate agent periodically
            if step % args.eval_freq == 0 and step > 0:
                L.log('eval/episode', episode, step)
                evaluate(env, agent, video, args.num_eval_episodes, L, step)
                if args.save_model:
                    agent.save(model_dir, step)
                if args.save_buffer:
                    replay_buffer.save(buffer_dir)

            L.log('train/episode_reward', episode_reward, step)

            env.step(np.array([0,0,0.1,0,0,0])) # Prevent getting stuck
            obs = env.reset()
            done, episode_reward, episode_step = False, 0, 0
            episode += 1

            L.log('train/episode', episode, step)

        # sample action for data collection
        if step < args.init_steps:
            action = env.action_space.sample()
        else:
            with utils.eval_mode(agent):
                action = agent.sample_action(obs)
                temp = action
                print("Temp action: {}".format(temp))
                action = np.multiply(action, env.action_space.high)

        # run training update
        if step >= args.init_steps:
            num_updates = args.init_steps if step == args.init_steps else 1
            for _ in range(num_updates):
                agent.update(replay_buffer, L, step)

        next_obs, reward, done, _ = env.step(action)
        print("E: {}   | S: {}   | R: {:.4f} | ER: {:.4f} | A: {}".format(episode, step, round(reward,4), round(episode_reward,4), action))

        # Reset environment if agent gets stuck (stuck means for 100 steps no increase in reward)
        if step % 100 == 0 and step > 0:
            if np.abs(prev_episode_reward - episode_reward) < 1e-5: # If change in reward is negligible after 100 steps restart
                env.step(np.array([0,0,0.1,0,0,0]))
                obs = env.reset()
            prev_episode_reward = episode_reward

        # allow infinit bootstrap
        done_bool = 0 if episode_step + 1 == env._max_episode_steps else float(
            done
        )
        episode_reward += reward

        replay_buffer.add(obs, action, reward, next_obs, done_bool)

        obs = next_obs
        episode_step += 1


if __name__ == '__main__':
    main()
