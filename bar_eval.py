import argparse
import torch
import time
import os
import numpy as np
import pickle
from types import SimpleNamespace
from gym.spaces import Box, Discrete
from pathlib import Path
from torch.autograd import Variable
from tensorboardX import SummaryWriter
from utils.make_env import make_env
from utils.buffer import ReplayBuffer, ReplayBufferATOC, ReplayBufferAttention
from utils.env_wrappers import SubprocVecEnv, DummyVecEnv
from utils.misc import plot_curve_with_label
from algorithms.maddpg import MADDPG
from algorithms.DTPC import DTPC
from algorithms.dqn import DQNs
from tqdm import tqdm
import random
import pylustrator
import matplotlib.pyplot as plt

pylustrator.start()
USE_CUDA = True# torch.cuda.is_available()
to_gpu=USE_CUDA
continue_train = False # 是否从当前训练结果，继续训练（注意buff没有保存，必须从新获取）
episode_num = 0
max_links = 3
Eval_episode = 5
metric_type = 'data_rate' #'reward'

def make_parallel_env(env_id, n_rollout_threads, seed, discrete_action):
    def get_env_fn(rank):
        def init_env():
            env = make_env(env_id, discrete_action=discrete_action)
            env.seed(seed + rank * 1000)
            np.random.seed(seed + rank * 1000)
            return env
        return init_env
    if n_rollout_threads == 1:
        return DummyVecEnv([get_env_fn(0)])
    else:
        return SubprocVecEnv([get_env_fn(i) for i in range(n_rollout_threads)])

def transf_params(params):
    save_params = dict()
    for var_name in params:
        if len(var_name)==0:
            continue
        if 'optim' in var_name:
            save_params.update({var_name:params[var_name]})
        else:
            temp_dict = {}
            for in_name in params[var_name]:
                temp_dict.update({in_name:params[var_name][in_name].shape})
            save_params.update({var_name:temp_dict})
    return save_params

def load_model(config, num_model):
    model_dir = Path('./models') / config.env_id / config.model_name / ('run'+str(num_model)) / 'model.pt'
    env = make_parallel_env(config.env_id, config.n_rollout_threads, config.seed,
                            config.discrete_action)
    if config.agent_alg == 'DTPC':
        AgentNet = DTPC.init_from_save(model_dir)
    elif config.agent_alg == 'maddpg' or config.agent_alg == 'ddpg':
        AgentNet = MADDPG.init_from_save(model_dir)
    elif config.agent_alg == 'dqn' or config.agent_alg == 'double_dqn' or config.agent_alg == 'dueling_dqn':
        AgentNet = DQNs.init_from_save(model_dir)

    return env, AgentNet

def evaluate_alg(config_file,num_model):
    model_dir = Path('./configs')
    config_dir = model_dir / config_file  # 'dqn_config.txt'
    with open(config_dir, 'r', encoding='utf-8') as f:
        # config = f.read()
        a = eval(f.read())
        config = SimpleNamespace(**a)

    env, AgentNet = load_model(config,num_model)
    # for ep_i in tqdm(range(0, config.n_episodes, config.n_rollout_threads)):
    mean_data_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_trans_p_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_vel_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_fly_p_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_reward_vs_time = np.zeros((Eval_episode, config.episode_length))
    for ep_i in range(Eval_episode):
        print(ep_i)
        obs_raw = env.reset()
        obs = np.array([[obs_raw[:, :, 0][0, i] for i in range(AgentNet.nagents)]])
        for et_i in range(config.episode_length):

            # rearrange observations to be per agent, and convert to torch Variable
            if config.agent_alg == 'maddpg' or config.agent_alg == 'ddpg' or config.agent_alg in ['dqn', 'double_dqn',
                                                                                                  'dueling_dqn']:
                torch_obs = [Variable(torch.Tensor(np.vstack(obs[:, i])),
                                      requires_grad=False)
                             for i in range(AgentNet.nagents)]
            else:
                torch_obs = Variable(torch.Tensor([obs[:, agent_idx, :] for agent_idx in range(AgentNet.nagents)]),
                                     requires_grad=False)
            torch_agent_actions = AgentNet.step(torch_obs, explore=True)
            # convert actions to numpy arrays
            agent_actions = [ac.data.numpy() for ac in torch_agent_actions]
            # rearrange actions to be per environment
            if config.agent_alg in ['dqn', 'double_dqn', 'dueling_dqn']:
                env.envs[0].discrete_action_space = True
                env.envs[0].discrete_action_input = True
            else:
                env.envs[0].discrete_action_space = False
                env.envs[0].discrete_action_input = False
            actions = [[ac[i] for ac in agent_actions] for i in range(config.n_rollout_threads)]

            obs_raw, rewards, dones, infos = env.step(actions)
            next_obs = np.array([[obs_raw[:, :, 0][0, i] for i in range(AgentNet.nagents)]])
            obs = next_obs
            # env.render()
            (num_sat_u, fly_p, trans_p, vel) = env.get_info()[0]
            mean_data_vs_time[ep_i, et_i] = num_sat_u
            mean_trans_p_vs_time[ep_i, et_i] = np.mean(trans_p)
            mean_fly_p_vs_time[ep_i, et_i] = np.mean(fly_p)
            mean_vel_vs_time[ep_i, et_i] = np.mean(vel)
            mean_reward_vs_time[ep_i, et_i] = np.mean(rewards)

            # print(env.get_info())
            # time.sleep(0.05)
    # rew += (np.sum(data_rate_list) - 5 * np.sum(power_consumption) - 0.1 * np.sum(mean_trans_p_vs_time))
    # mean reward
    # return np.mean(mean_data_vs_time, axis=0) - 5* np.mean(mean_fly_p_vs_time, axis=0) - 0.1 * np.mean(mean_trans_p_vs_time, axis=0)
    # return np.mean(mean_reward_vs_time, axis=0)

    # mean_satisfied users
    if metric_type == 'reward':
        return np.mean(mean_reward_vs_time, axis=0)
    else:
        return np.mean(mean_data_vs_time), np.mean(mean_trans_p_vs_time)
    # plot_curve_with_label(np.expand_dims(np.mean(mean_data_vs_time, axis=0), 0), 'num_satisfied_users',
    #                       (34.0 / 255.0, 171.0 / 255.0, 244.0 / 255.0))
    # plot_curve_with_label(np.expand_dims(np.mean(mean_trans_p_vs_time, axis=0), 0), 'mean_transmit_power',
    #                       (34.0 / 255.0, 171.0 / 255.0, 244.0 / 255.0))
    # plot_curve_with_label(np.expand_dims(np.mean(mean_vel_vs_time, axis=0), 0), 'mean_velocity',
    #                       (34.0 / 255.0, 171.0 / 255.0, 244.0 / 255.0))
def cummean(array_in):
    return np.cumsum(array_in) / np.arange(1,26)
def evaluate(config):
    import matplotlib.pyplot as plt
    fig = plt.figure()
    ax1 = fig.add_subplot(1, 1, 1)
    # ax2 = ax1.twinx()
    t = np.linspace(0, 24, 25)+1
    #ax1.plot(t, np.mean(mean_data_vs_time,axis=0), 'b-')
    # 'DDPG','MADDPG','DQN','Double DQN','Dueling DQN','random','Static'
    path_temp = Path('./models') / config.env_id /'_algorithm_reward.csv'
    mode_temp = 'w'
    if os.path.exists(path_temp):
        mode_temp = 'a'
    marker_list = ['.',  #point
                   '+',  # plus
                   'x',  # x
                   'D',  # diamond
                   'v',  # triangle_down
                   '^',  # triangle_up
                   '<',  # triangle_left
                   '>'  # triangle_right
                   ]
    line_list = ['-' ,  #solid line style
                '--',  #dashed line style
                '-.',  #dash-dot line style
                ':',  #dotted line style
                '-',  # solid line style
                '--',  # dashed line style
                '-.',  # dash-dot line style
                ':'  # dotted line style
                ]
    color_list  =['b', #blue
                'g', #green
                'r', #red
                'c', #cyan
                'm', #magenta
                'y', #yellow
                'k', #black
                'w' #white
                ]
    # label_list = ['MADDPG','DTPC', 'DDPG-[13]', 'Double DQN-[12]', 'Dueling DQN-[10]','Fixed']
    # alg_idx = [1,1,1,1,1,1]
    label_list = ['MADDPG','DTPC', 'Fixed']
    alg_idx = [1,1,1]
    satisfied_users_lst = []
    mean_trans_power_lst = []
    DTPC_NO = 18*64+64*64+ 64*64+64*3   +  (18+3)*4*128+ 128*128 + 128*128 +128*1
    MADDPG_NO = 18*64+64*64+ 64*64+64*3   + 4*(  (18+3)*4*128+ 128*128 + 128*128 +128*1)
    paralist = [1,# MADDPG
                 DTPC_NO/MADDPG_NO, # DTPC
                0]
    with open(path_temp, mode_temp) as f:
        for i_curve, txt_file in enumerate(['maddpg_config.txt','dtpc_config.txt']):
            data, power = evaluate_alg(txt_file, alg_idx[i_curve])
            satisfied_users_lst.append(np.around(data, 1))
            mean_trans_power_lst.append(np.around(power, 2))

        data, power = uniform_evaluate(config)
        satisfied_users_lst.append(np.around(data, 1))
        mean_trans_power_lst.append(np.around(power, 2))

    mean_trans_power_lst_norm = np.array(mean_trans_power_lst)/np.max(mean_trans_power_lst)
    satisfied_users_lst_norm = np.array(satisfied_users_lst)/np.max(satisfied_users_lst)
    labels = ['Satisfied UE No.', 'Transmit Power', 'Model Parameters']


    x = np.arange(len(labels))  # the label locations
    width = 0.15 # the width of the bars

    fig, ax = plt.subplots()
    #idx_offset = [-2.0,-1.0,0.0,1.0,2.0]
    #idx_offset = [-5.0/2, -3.0/2, -1.0/2, 1.0/2, 3.0/2, 5.0/2]
    idx_offset = [-1.0,0.0,1.0]
    for i in range(3):
        rects = ax.bar(x - idx_offset[i]*width, [satisfied_users_lst_norm[i],mean_trans_power_lst_norm[i],paralist[i]], width, label=label_list[i])

        ax.bar_label(rects, labels = [satisfied_users_lst[i], mean_trans_power_lst[i], paralist[i]*MADDPG_NO], padding=3)

    # Add some text for labels, title and custom x-axis tick labels, etc.
    ax.set_ylabel('Performance')
    ax.set_xticks(x)
    ax.set_xticklabels(labels)

    ax.legend()

    #% start: automatic generated code from pylustrator
    plt.figure(2).ax_dict = {ax.get_label(): ax for ax in plt.figure(2).axes}
    import matplotlib as mpl
    plt.figure(2).axes[0].set_position([0.054536, 0.082755, 0.879839, 0.886898])
    plt.figure(2).axes[0].get_legend()._set_loc((0.408283, 0.582268))
    plt.figure(2).axes[0].texts[8].set_visible(False)
    plt.figure(2).axes[0].texts[11].set_visible(False)
    plt.figure(2).axes[0].texts[14].set_visible(False)
    #% end: automatic generated code from pylustrator
    fig.tight_layout()
    plt.yticks([])
    plt.show()
    #
    #
    # ax1.legend(loc="best")
    # ax1.grid()
    # ax1.set_facecolor("white")
    # ax1.set_ylabel('Avg. satisfied users No.',fontsize='large')
    # ax1.set_xlabel('Time slot in an episode', fontsize='large')
    #
    # fig.patch.set_facecolor('white')
    #
    # #% start: automatic generated code from pylustrator
    # plt.figure(1).ax_dict = {ax.get_label(): ax for ax in plt.figure(1).axes}
    # import matplotlib as mpl
    # plt.figure(1).axes[0].legend(borderpad=0.19999999999999998, handlelength=2.5, handletextpad=1.0, fontsize=8.0, title_fontsize=10.0)
    # plt.figure(1).axes[0].set_position([0.125000, 0.110000, 0.775000, 0.770000])
    # plt.figure(1).axes[0].get_legend()._set_loc((0.681340, 0.421367))
    # plt.figure(1).axes[0].get_legend()._set_loc((0.677308, 0.407839))
    # plt.figure(1).axes[0].get_legend()._set_loc((0.667227, 0.413250))
    # #% end: automatic generated code from pylustrator
    # plt.show()
    plt.savefig('learning_curve.pdf', dpi=400)
    return 0

def uniform_evaluate(config):

    env = make_parallel_env(config.env_id, config.n_rollout_threads, config.seed,
                            config.discrete_action)
    mean_data_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_fly_p_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_trans_p_vs_time = np.zeros((Eval_episode, config.episode_length))
    mean_reward_vs_time = np.zeros((Eval_episode, config.episode_length))
    for ep_i in range(Eval_episode):
        obs_raw = env.reset()
        for et_i in range(config.episode_length):
            #env.render()
            # landmark.state.p_pos = np.random.uniform(-500, +500, world.dim_p)
            env.envs[0].discrete_action_space = False
            env.envs[0].discrete_action_input = False
            # if config.agent_alg in ['dqn', 'double_dqn', 'dueling_dqn']:
            #     actions = [[9,9,9,9]]
            # else:
            actions = [[np.array([0.0, 0.0, 1.0]), np.array([0.0, 0.0, 1.0]),
                            np.array([0.0, 0.0, 1.0]), np.array([0.0, 0.0, 1.0])]]
            #actions = [[np.array([0,0,1],dtype=np.float64),np.array([0,0,1],dtype=np.float64),np.array([0,0,1],dtype=np.float64),np.array([0,0,1],dtype=np.float64)]]
            obs_raw, rewards, dones, infos = env.step(actions)
            (num_sat_u, fly_p, trans_p, _) = env.get_info()[0]

            mean_data_vs_time[ep_i, et_i] = num_sat_u
            mean_fly_p_vs_time[ep_i, et_i] = np.mean(fly_p)
            mean_trans_p_vs_time[ep_i, et_i] = np.mean(trans_p)
            mean_reward_vs_time[ep_i, et_i] = np.mean(rewards)

    # return np.mean(mean_reward_vs_time, axis=0)
    # return np.mean(mean_data_vs_time, axis=0) - 5 * np.mean(mean_fly_p_vs_time, axis=0) - 0.1 * np.mean(
    #     mean_trans_p_vs_time, axis=0)
    if metric_type == 'reward':
        return np.mean(mean_reward_vs_time, axis=0)
    else:
        return np.mean(mean_data_vs_time), np.mean(mean_trans_p_vs_time)

if __name__ == '__main__':
    config = SimpleNamespace()
    config.env_id = 'uav_com'
    config.n_rollout_threads = 1
    config.seed = 901
    config.discrete_action = False
    config.episode_length = 25
    evaluate(config)
