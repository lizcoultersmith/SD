import os
import torch
import torch.nn as nn
from torch.utils.checkpoint import checkpoint

from transformers import T5Tokenizer, T5EncoderModel, CLIPTokenizer, CLIPTextModel

# !!!
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '../../../xtra'))

import open_clip
from ldm.util import default, count_params


def _expand_mask(mask, dtype, tgt_len=None):
    """ Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]` """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len
    expanded_mask = (mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype))
    inverted_mask = 1.0 - expanded_mask
    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)

def _build_causal_attention_mask(bsz, seq_len, dtype):
    # lazily create causal attention mask, with full attention between the vision tokens
    # pytorch uses additive attention mask; fill with -inf
    mask = torch.empty(bsz, seq_len, seq_len, dtype=dtype)
    mask.fill_(torch.tensor(torch.finfo(dtype).min))
    mask.triu_(1)  # zero out the lower diagonal
    mask = mask.unsqueeze(1)  # expand mask
    return mask

class AbstractEncoder(nn.Module):
    def __init__(self):
        super().__init__()

    def encode(self, *args, **kwargs):
        raise NotImplementedError

class IdentityEncoder(AbstractEncoder):

    def encode(self, x):
        return x


class ClassEmbedder(nn.Module):
    def __init__(self, embed_dim, n_classes=1000, key='class', ucg_rate=0.1):
        super().__init__()
        self.key = key
        self.embedding = nn.Embedding(n_classes, embed_dim)
        self.n_classes = n_classes
        self.ucg_rate = ucg_rate

    def forward(self, batch, key=None, disable_dropout=False):
        if key is None:
            key = self.key
        # this is for use in crossattn
        c = batch[key][:, None]
        if self.ucg_rate > 0. and not disable_dropout:
            mask = 1. - torch.bernoulli(torch.ones_like(c) * self.ucg_rate)
            c = mask * c + (1-mask) * torch.ones_like(c)*(self.n_classes-1)
            c = c.long()
        c = self.embedding(c)
        return c

    def get_unconditional_conditioning(self, bs, device="cuda"):
        uc_class = self.n_classes - 1  # 1000 classes --> 0 ... 999, one extra class for ucg (class 1000)
        uc = torch.ones((bs,), device=device) * uc_class
        uc = {self.key: uc}
        return uc


def disabled_train(self, mode=True):
    """Overwrite model.train with this function to make sure train/eval mode
    does not change anymore."""
    return self


class FrozenT5Embedder(AbstractEncoder):
    """Uses the T5 transformer encoder for text"""
    def __init__(self, version="google/t5-v1_1-large", device="cuda", max_length=77, freeze=True):  # others are google/t5-v1_1-xl and google/t5-v1_1-xxl
        super().__init__()
        self.tokenizer = T5Tokenizer.from_pretrained(version)
        self.transformer = T5EncoderModel.from_pretrained(version)
        self.device = device
        self.max_length = max_length   # TODO: typical value?
        if freeze:
            self.freeze()

    def freeze(self):
        self.transformer = self.transformer.eval()
        #self.train = disabled_train
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text):
        batch_encoding = self.tokenizer(text, truncation=True, max_length=self.max_length, return_length=True,
                                        return_overflowing_tokens=False, padding="max_length", return_tensors="pt")
        tokens = batch_encoding["input_ids"].to(self.device)
        outputs = self.transformer(input_ids=tokens)

        z = outputs.last_hidden_state
        return z

    def encode(self, text):
        return self(text)


class FrozenCLIPEmbedder(AbstractEncoder):
    """Uses the CLIP transformer encoder for text (from huggingface)"""
    LAYERS = ["last", "pooled", "hidden"]
    # clip-vit-base-patch32
# !!!
    def __init__(self, version="openai/clip-vit-large-patch14", device="cuda", max_length=77, freeze=True, layer="last", layer_idx=None, model_dir='models'):
        super().__init__()
        assert layer in self.LAYERS
        self.tokenizer = CLIPTokenizer.from_pretrained(version, cache_dir=os.path.join(model_dir, os.path.basename(version)), local_files_only=False)
        self.transformer = CLIPTextModel.from_pretrained(version, cache_dir=os.path.join(model_dir, os.path.basename(version)), local_files_only=False)
        self.device = device
        self.max_length = max_length
        if freeze:
            self.freeze()

        self.layer = layer
        self.layer_idx = layer_idx
        if layer == "hidden":
            assert layer_idx is not None
            assert 0 <= abs(layer_idx) <= 12

        def embedding_forward(self, input_ids=None, position_ids=None, inputs_embeds=None, embedding_manager=None) -> torch.Tensor:
            seq_length = (input_ids.shape[-1] if input_ids is not None else inputs_embeds.shape[-2])
            if inputs_embeds is None:
                inputs_embeds = self.token_embedding(input_ids)
            if embedding_manager is not None:
                inputs_embeds = embedding_manager(input_ids, inputs_embeds)
            if position_ids is None:
                position_ids = self.position_ids[:, :seq_length]
            position_embeddings = self.position_embedding(position_ids)
            embeddings = inputs_embeds + position_embeddings
            return embeddings

        self.transformer.text_model.embeddings.forward = (embedding_forward.__get__(self.transformer.text_model.embeddings))

        def encoder_forward(self, inputs_embeds, attention_mask=None, causal_attention_mask=None, \
                            output_attentions=None, output_hidden_states=None, return_dict=None):
            output_attentions = (output_attentions if output_attentions is not None else self.config.output_attentions)
            output_hidden_states = (output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states)
            return_dict = (return_dict if return_dict is not None else self.config.use_return_dict)

            encoder_states = () if output_hidden_states else None
            all_attentions = () if output_attentions else None

            hidden_states = inputs_embeds
            for idx, encoder_layer in enumerate(self.layers):
                if output_hidden_states:
                    encoder_states = encoder_states + (hidden_states,)
                layer_outputs = encoder_layer(hidden_states, attention_mask, causal_attention_mask, output_attentions=output_attentions)
                hidden_states = layer_outputs[0]
                if output_attentions:
                    all_attentions = all_attentions + (layer_outputs[1],)

            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)

            return hidden_states

        self.transformer.text_model.encoder.forward = encoder_forward.__get__(self.transformer.text_model.encoder)

        def text_encoder_forward(self, input_ids=None, attention_mask=None, position_ids=None, output_attentions=None, output_hidden_states=None, \
                                 return_dict=None, embedding_manager=None):
            output_attentions = (output_attentions if output_attentions is not None else self.config.output_attentions)
            output_hidden_states = (output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states)
            return_dict = (return_dict if return_dict is not None else self.config.use_return_dict)

            if input_ids is None:
                raise ValueError('You have to specify either input_ids')

            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])

            hidden_states = self.embeddings(input_ids=input_ids, position_ids=position_ids, embedding_manager=embedding_manager)

            bsz, seq_len = input_shape
            # CLIP's text model uses causal mask, prepare it here.
            # https://github.com/openai/CLIP/blob/cfcffb90e69f37bf2ff1e988237a0fbe41f33c04/clip/model.py#L324
            causal_attention_mask = _build_causal_attention_mask(bsz, seq_len, hidden_states.dtype).to(hidden_states.device)

            # expand attention_mask
            if attention_mask is not None:
                attention_mask = _expand_mask(attention_mask, hidden_states.dtype) # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]

            last_hidden_state = self.encoder(inputs_embeds=hidden_states, attention_mask=attention_mask, causal_attention_mask=causal_attention_mask, \
                                             output_attentions=output_attentions, output_hidden_states=output_hidden_states, return_dict=return_dict)
            last_hidden_state = self.final_layer_norm(last_hidden_state)
            return last_hidden_state

        self.transformer.text_model.forward = text_encoder_forward.__get__(self.transformer.text_model)

        def transformer_forward(self, input_ids=None, attention_mask=None, position_ids=None, output_attentions=None, output_hidden_states=None, \
                                return_dict=None, embedding_manager=None):
            return self.text_model(input_ids=input_ids, attention_mask=attention_mask, position_ids=position_ids, output_attentions=output_attentions, \
                                   output_hidden_states=output_hidden_states, return_dict=return_dict, embedding_manager=embedding_manager)

        self.transformer.forward = transformer_forward.__get__(self.transformer)

    def freeze(self):
        self.transformer = self.transformer.eval()
        #self.train = disabled_train
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text, **kwargs):
        batch_encoding = self.tokenizer(text, truncation=True, max_length=self.max_length, return_length=True,
                                        return_overflowing_tokens=False, padding="max_length", return_tensors="pt")
        tokens = batch_encoding["input_ids"].to(self.device)

        outputs = self.transformer(input_ids=tokens, output_hidden_states=self.layer=="hidden", **kwargs)
        if self.layer == "last" and hasattr(outputs, 'last_hidden_state'):
            z = outputs.last_hidden_state
        elif self.layer == "pooled" and hasattr(outputs, 'pooler_output'):
            z = outputs.pooler_output[:, None, :]
        elif hasattr(outputs, 'hidden_states'):
            z = outputs.hidden_states[self.layer_idx]
        else:
            z = outputs # only this works - after adding embeddings
        return z

    def encode(self, text, **kwargs):
        return self(text, **kwargs)


class FrozenOpenCLIPEmbedder(AbstractEncoder):
    """ Uses the OpenCLIP transformer encoder for text """
    LAYERS = [
        #"pooled",
        "last",
        "penultimate"
    ]
# !!!
    def __init__(self, arch="ViT-H-14", version="laion2b_s32b_b79k", device="cuda", max_length=77, freeze=True, layer="last", model_dir='models'):
        super().__init__()
        assert layer in self.LAYERS
        model_path = os.path.join(model_dir, 'openclip', 'open_clip_pytorch_model.bin')
        model, _, _ = open_clip.create_model_and_transforms(arch, device=torch.device('cpu'), pretrained=model_path)
        del model.visual
        self.model = model

        self.device = device
        self.max_length = max_length
        if freeze:
            self.freeze()
        self.layer = layer
        if self.layer == "last":
            self.layer_idx = 0
        elif self.layer == "penultimate":
            self.layer_idx = 1
        else:
            raise NotImplementedError()

    def freeze(self):
        self.model = self.model.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text):
        tokens = open_clip.tokenize(text)
        z = self.encode_with_transformer(tokens.to(self.device))
        return z

    def encode_with_transformer(self, text):
        x = self.model.token_embedding(text)  # [batch_size, n_ctx, d_model]
        x = x + self.model.positional_embedding
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.text_transformer_forward(x, attn_mask=self.model.attn_mask)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.model.ln_final(x)
        return x

    def text_transformer_forward(self, x: torch.Tensor, attn_mask = None):
        for i, r in enumerate(self.model.transformer.resblocks):
            if i == len(self.model.transformer.resblocks) - self.layer_idx:
                break
            if self.model.transformer.grad_checkpointing and not torch.jit.is_scripting():
                x = checkpoint(r, x, attn_mask)
            else:
                x = r(x, attn_mask=attn_mask)
        return x

    # TODO Fix embedding manager
    def encode(self, text, embedding_manager=None):
        return self(text)


class FrozenCLIPT5Encoder(AbstractEncoder):
    def __init__(self, clip_version="openai/clip-vit-large-patch14", t5_version="google/t5-v1_1-xl", device="cuda",
                 clip_max_length=77, t5_max_length=77):
        super().__init__()
        self.clip_encoder = FrozenCLIPEmbedder(clip_version, device, max_length=clip_max_length)
        self.t5_encoder = FrozenT5Embedder(t5_version, device, max_length=t5_max_length)
        # print(f"{self.clip_encoder.__class__.__name__} has {count_params(self.clip_encoder)*1.e-6:.2f} M parameters, "
              # f"{self.t5_encoder.__class__.__name__} comes with {count_params(self.t5_encoder)*1.e-6:.2f} M params.")

    def encode(self, text):
        return self(text)

    def forward(self, text):
        clip_z = self.clip_encoder.encode(text)
        t5_z = self.t5_encoder.encode(text)
        return [clip_z, t5_z]


