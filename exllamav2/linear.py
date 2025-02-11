import torch
import torch.nn.functional as F
from exllamav2.module import ExLlamaV2Module
from torch import nn
from exllamav2 import ext
from exllamav2.ext import exllamav2_ext as ext_c, none_tensor
from safetensors import safe_open


class ExLlamaV2Linear(ExLlamaV2Module):

    in_features: int
    out_features: int
    has_bias: bool

    linear: nn.Linear or None = None
    q_handle: int or None = None
    q_tensors: dict or None = None

    name: str = "Linear"

    temp_dq: torch.tensor
    padding: int = 0

    lora_a_tensors: dict
    lora_b_tensors: dict

    def __init__(self, model, key, in_features, out_features, has_bias, pad32 = True):
        super().__init__(model, key)

        if pad32: self.padding = -out_features % 32

        self.in_features = in_features
        self.out_features = out_features + self.padding
        self.has_bias = has_bias
        self.temp_dq = None
        self.footprint = -1

        self.lora_a_tensors = {}
        self.lora_b_tensors = {}


    def load(self, w = None):

        if w is None: w = self.load_weight()
        if isinstance(w, dict):
            if self.has_bias:
                assert "bias" in w, self.key + " has no bias but bias expected"
            else:
                assert "bias" not in w, self.key + " has bias but bias is not expected"
            device_tensors = self.model.get_device_tensors(self.device_idx)
            device_tensors.begin_scratch_alloc()
            self.temp_dq = device_tensors.get_scratch_slice(self.temp_dq_size())
            self.q_tensors = w
            self.q_handle = ext.make_q_matrix(w, self.temp_dq)

        elif isinstance(w, nn.Parameter):
            assert not self.has_bias, self.key + " has no bias tensor but bias is expected"
            if self.padding > 0: w = nn.Parameter(F.pad(w.data, (0, 0, 0, self.padding)).contiguous())
            self.linear = nn.Linear(self.in_features, self.out_features, self.has_bias, device = "meta", dtype = torch.float16)
            self.linear.weight = w

        elif isinstance(w, tuple):
            assert self.has_bias, self.key + " has bias tensor but bias is not expected"
            ww = w[0]
            wb = w[1]
            if self.padding > 0:
                ww = nn.Parameter(F.pad(ww.data, (0, 0, 0, self.padding)).contiguous())
                wb = nn.Parameter(F.pad(wb.data, (0, 0, 0, self.padding)).contiguous())
            self.linear = nn.Linear(self.in_features, self.out_features, self.has_bias, device = "meta", dtype = torch.float16)
            self.linear.weight = ww
            self.linear.bias = wb


    def matrix_shape(self):

        return self.in_features, self.out_features


    def numel(self):

        return self.in_features * self.out_features


    def unload(self):

        if self.linear is not None:
            del self.linear
            self.linear = None

        if self.q_handle is not None:
            ext_c.free_q_matrix(self.q_handle)
            self.q_handle = None

        if self.q_tensors is not None:
            for k, v in self.q_tensors.items(): del v
            self.q_tensors = None

        self.temp_dq = None


    def get_weight(self):

        return self.linear.weight.data


    def scratch_space_fixed(self):

        return self.temp_dq_size() + \
               self.temp_fwd_size()


    def scratch_space(self):

        return self.temp_dq_size() + \
               self.temp_fwd_size()


    def temp_dq_size(self):

        return self.in_features * self.out_features * 2 + 128


    def temp_fwd_size(self):

        return self.out_features * self.model.config.max_input_len * self.model.config.max_batch_size * 4 + 128


    def forward(self, hidden_states, cache = None, attn_params = None, past_len = None, intermediates = False, loras = None, force_recons = False, force_cuda = False):

        # Linear forward

        if self.q_handle is not None and not force_recons:

            output_shape = hidden_states.shape[:-1] + (self.out_features,)
            hidden_states = hidden_states.view(-1, hidden_states.shape[-1])
            # hidden_states = hidden_states[:, self.q_tensors["q_perm"]]
            output = torch.empty((hidden_states.shape[0], self.out_features), dtype = torch.half, device = self.device())
            ext_c.gemm_half_q_half(hidden_states, self.q_handle, output, force_cuda)

            hidden_states_out = output.view(output_shape)

        else:

            matrix = self.get_weight_tensor_dq()
            hidden_states_out = torch.matmul(hidden_states, matrix)

            if self.has_bias:
                bias = self.get_bias_tensor()
                hidden_states_out += bias

        # Evaluate LoRAs

        if loras is not None:
            for lora in loras:
                lora_a = self.lora_a_tensors.get(lora)
                lora_b = self.lora_b_tensors.get(lora)
                if lora_a is not None:
                    assert lora_b is not None
                    temp = torch.matmul(hidden_states, lora_a)
                    hidden_states_out += torch.matmul(temp, lora_b)

        if intermediates:
            return {"hidden_states": hidden_states_out}
        else:
            return hidden_states_out


    # def dump_group_info(self):
    #
    #     if "q_groups" in self.q_tensors:
    #
    #         groups = self.q_tensors["q_groups"].cpu()
    #
    #         if "q_invperm" in self.q_tensors:
    #             height = self.q_tensors["q_invperm"].shape[0]
    #         else:
    #             height = self.q_tensors["q_weight"].shape[0] * 8
    #
    #         groupsize = 1
    #         while groupsize * groups.shape[0] / 2 < height:
    #             groupsize *= 2
    #
    #         gis = f"gs: {groupsize}, "
    #         i = 0
    #         pg = 0
    #         gc = 0
    #         while i <= groups.shape[0]:
    #             g = groups[i].item() if i < groups.shape[0] else -1
    #             if g != pg:
    #                 if pg != 0:
    #                     gis += f"{pg}: {gc}, "
    #                     gc = 0
    #                 pg = g
    #             gc += 1
    #             i += 2
    #
    #         return gis
    #
    #     else:
    #
    #         return "GPTQ"


    def get_weight_tensor_dq(self):

        if self.linear is not None:
            return self.linear.weight.data.T

        elif self.q_handle is not None:
            tensor = torch.empty((self.in_features, self.out_features), dtype = torch.half, device = self.device())
            ext_c.reconstruct(self.q_handle, tensor)
            return tensor
            # ext_c.reconstruct(self.q_handle, self.temp_dq)
            # return self.temp_dq

        else:
            raise ValueError(f"Layer {self.key} has no data")


    def get_bias_tensor(self):

        if self.linear is not None:
            return self.linear.bias.data

        elif self.q_handle is not None:
            return self.q_tensors["bias"]

        else:
            raise ValueError(f"Layer {self.key} has no data")

    def is_quant(self):

        return self.q_handle is not None


    def rank_reduce(self, k):

        assert not self.is_quant(), "Can't rank-reduce quantized layer"

        weight = self.linear.weight.data.float()
        max_rank = min(weight.shape[0], weight.shape[1])
        desired_rank = int(max_rank * k)
        results = torch.svd_lowrank(weight, q = desired_rank, niter = 10)
        weight_approx = results[0] @ torch.diag(results[1]) @ results[2].T

        self.linear.weight = nn.Parameter(weight_approx.half())
