
import math
import sys

import torch
from torch import nn

from utils.nets.graph_encoder import GraphAttentionEncoder
from utils.nets.critic import ValueNet
sys.path.append('../')
from utils.problems.problem_tsp import TSP


class AM(nn.Module):
    def __init__(self, cfg):
        super(AM, self).__init__()

        self.embedding_dim = cfg.embedding_dim
        self.hidden_dim = cfg.hidden_dim
        self.n_encode_layers = cfg.n_layers_encoder
        self.decode_type = None
        self.temp = 1
        self.tanh_clipping = cfg.tanh_clipping
        self.n_heads = cfg.n_heads

        
        # Learned input symbols for first action
        self.W_placeholder = nn.Parameter(torch.Tensor(2 * self.embedding_dim))
        self.W_placeholder.data.uniform_(-1, 1)  # Placeholder should be in range of activations

        self.initial_embedder = nn.Linear(2, self.embedding_dim)

        self.encoder = GraphAttentionEncoder(
            n_heads=self.n_heads,
            embed_dim=self.embedding_dim,
            n_layers=self.n_encode_layers,
            normalization=cfg.normalization
        )

        self.project_node_embeddings = nn.Linear(self.embedding_dim, 3 * self.embedding_dim, bias=False)
        self.project_fixed_context = nn.Linear(self.embedding_dim, self.embedding_dim, bias=False)
        self.project_step_context = nn.Linear(2*cfg.embedding_dim, self.embedding_dim, bias=False)
        self.project_out = nn.Linear(self.embedding_dim, self.embedding_dim, bias=False)
        self.critic = ValueNet(cfg)

    def set_decode_type(self, decode_type, temp=None):
        self.decode_type = decode_type
        if temp is not None:  # Do not change temperature if not provided
            self.temp = temp

    def forward(self, input, tour_to_be_evaluated=None):
        '''
        input: (batch, node, 2)
        output: 
            cost: (batch, node)
            log_p_total: (batch, node)
            value: (batch, node)
        '''
        points_embedded = self.initial_embedder(input)
        embeddings, _ = self.encoder(points_embedded)

        output_decoder = self.decoder(input, embeddings, tour_to_be_evaluated)
        log_p, instant_reward, value, cost, reward_final, tour = output_decoder
        return log_p, instant_reward, value, cost, reward_final, tour


    def _calc_log_likelihood(self, log_p, tour):

        log_p = log_p.gather(2, tour.unsqueeze(-1)).squeeze(-1) # (batch*sample, node)
        log_p_total = log_p.sum(1) # (batch*sample)
        return log_p_total


    def calc_distance(self, points, current, previous):
        batch_id = torch.arange(points.size(0))
        point_current = points[batch_id, current]
        point_previous = points[batch_id, previous]
        dif = point_current - point_previous
        dif2 = dif.pow(2)
        distances = dif2.sum(dim=1).sqrt() # (batch)
        return distances


    def decoder(self, input, embeddings, tour_to_be_evaluated=None):

        log_ps = []
        instant_rewards = []
        values = []
        tours = []

        state = TSP.make_state(input)

        graph_embedding, key_glimpse, val_glimpse, logit_key = self._precompute(embeddings)

        i = 0
        batch_size, n_node, _ = input.shape
        batch_id = torch.arange(batch_size)

        while not state.all_finished():
            first_and_last = self._get_parallel_step_context(embeddings, state)
            log_p, mask = self._get_log_p(first_and_last, graph_embedding, key_glimpse, 
                                          val_glimpse, logit_key, state)
            if tour_to_be_evaluated is None:
                selected = self._select_node(log_p.exp()[:, 0, :], mask[:, 0, :])
            else:
                selected = tour_to_be_evaluated[:, i]
            log_p_selected = log_p.squeeze()[batch_id, selected]
            mask = mask.transpose(1, 2).expand_as(embeddings)
            available = embeddings[~mask].view(batch_size, n_node-i, -1) # (batch, node-i_step, emb)
            value = self.critic(available, first_and_last) # (batch)
            if i==0:
                instant_reward = torch.zeros_like(value).detach()
            if 0<i:
                instant_reward = -self.calc_distance(input, selected, previous)
            instant_rewards.append(instant_reward)

            values.append(value)
            log_ps.append(log_p_selected)
            previous = selected
            tours.append(selected)

            state = state.update(selected)
            i += 1

        reward_final = -self.calc_distance(input, selected, tours[0])
        log_ps = torch.stack(log_ps, 1)
        instant_rewards = torch.stack(instant_rewards, 1) # (batch, node)
        tours = torch.stack(tours, 1)
        cost, mask = TSP.get_costs(input, tours)
        values = torch.stack(values, 1)
        return log_ps, instant_rewards, values, cost, reward_final, tours



    def _select_node(self, probs, mask):

        if self.decode_type == "greedy":
            _, selected = probs.max(1)
            assert not mask.gather(1, selected.unsqueeze(
                -1)).data.any(), "Decode greedy: infeasible action has maximum probability"

        elif self.decode_type == "sampling":
            selected = probs.multinomial(1).squeeze(1)

            while mask.gather(1, selected.unsqueeze(-1)).data.any():
                print('Sampled bad values, resampling!')
                selected = probs.multinomial(1).squeeze(1)
        else:
            raise NotImplementedError
        return selected

    def _precompute(self, embeddings):

        # The fixed context projection of the graph embedding is calculated only once for efficiency
        graph_embed = embeddings.mean(1)
        # fixed context = (batch_size, 1, embed_dim) to make broadcastable with parallel timesteps
        graph_embedding = self.project_fixed_context(graph_embed)[:, None, :]

        # The projection of the node embeddings for the attention is calculated once up front
        glimpse_key_fixed, glimpse_val_fixed, logit_key_fixed = \
            self.project_node_embeddings(embeddings[:, None, :, :]).chunk(3, dim=-1)

        # No need to rearrange key for logit as there is a single head
        key_glimpse = self._make_heads(glimpse_key_fixed, 1)
        val_glimpse = self._make_heads(glimpse_val_fixed, 1)
        logit_key = logit_key_fixed.contiguous()
        return graph_embedding, key_glimpse, val_glimpse, logit_key


    def _get_log_p(self, first_and_last, graph_embedding, 
                   key_glimpse, val_glimpse, logit_key, state):
        
        # Compute query = context node embedding
        query = graph_embedding + self.project_step_context(first_and_last)

        # Compute keys and values for the nodes
        glimpse_K, glimpse_V, logit_K = key_glimpse, val_glimpse, logit_key

        # Compute the mask
        mask = state.get_mask()

        # Compute logits (unnormalized log_p)
        log_p, glimpse = self._one_to_many_logits(query, glimpse_K, glimpse_V,
                                                logit_K, mask)

        log_p = torch.log_softmax(log_p / self.temp, dim=-1)
        assert not torch.isnan(log_p).any()

        return log_p, mask

    def _get_parallel_step_context(self, embeddings, state):

        current_node = state.get_current_node()
        batch_size = current_node.size(0)
        
        if state.i.item() == 0:
            return self.W_placeholder[None, None, :].expand(batch_size, 1, 
                                                            self.W_placeholder.size(-1))

        index_first_last = torch.cat((state.first_a, current_node), 1)[:, :, None]
        index_first_last = index_first_last.expand(batch_size, 2, embeddings.size(-1))
        first_last = embeddings.gather(1, index_first_last)
        first_last = first_last.view(batch_size, 1, -1)
        return first_last


    def _one_to_many_logits(self, query, glimpse_K, glimpse_V, logit_K, mask):

        batch_size, num_steps, embed_dim = query.size()
        key_size = val_size = embed_dim // self.n_heads

        glimpse_Q = query.view(batch_size, num_steps, self.n_heads, 1, key_size).permute(2, 0, 1, 3, 4)
        query_attention = torch.matmul(glimpse_Q, glimpse_K.transpose(-2, -1))
        compatibility =  query_attention / math.sqrt(glimpse_Q.size(-1))

        heads = torch.matmul(torch.softmax(compatibility, dim=-1), glimpse_V)

        heads = heads.permute(1, 2, 3, 0, 4).contiguous().view(-1, num_steps, 1, embed_dim)
        glimpse = self.project_out(heads)

        final_Q = glimpse
        logits = torch.matmul(final_Q, logit_K.transpose(-2, -1)).squeeze(-2) / math.sqrt(final_Q.size(-1))

        logits = torch.tanh(logits) * self.tanh_clipping
        logits[mask] = -math.inf

        return logits, glimpse.squeeze(-2)


    def _make_heads(self, v, num_steps=None):
        assert num_steps is None or v.size(1) == 1 or v.size(1) == num_steps

        return (
            v.contiguous().view(v.size(0), v.size(1), v.size(2), self.n_heads, -1)
            .expand(v.size(0), v.size(1) if num_steps is None else num_steps, v.size(2), self.n_heads, -1)
            .permute(3, 0, 1, 2, 4)  # (n_heads, batch_size, num_steps, graph_size, head_dim)
        )