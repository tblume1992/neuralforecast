# AUTOGENERATED! DO NOT EDIT! File to edit: ../../nbs/models.tft.ipynb.

# %% auto 0
__all__ = ['TFT']

# %% ../../nbs/models.tft.ipynb 4
from typing import Tuple, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torch.nn import LayerNorm

from ..losses.pytorch import MAE
from ..common._base_model import BaseModel

# %% ../../nbs/models.tft.ipynb 10
class MaybeLayerNorm(nn.Module):
    def __init__(self, output_size, hidden_size, eps):
        super().__init__()
        if output_size and output_size == 1:
            self.ln = nn.Identity()
        else:
            self.ln = LayerNorm(output_size if output_size else hidden_size, eps=eps)

    def forward(self, x):
        return self.ln(x)


class GLU(nn.Module):
    def __init__(self, hidden_size, output_size):
        super().__init__()
        self.lin = nn.Linear(hidden_size, output_size * 2)

    def forward(self, x: Tensor) -> Tensor:
        x = self.lin(x)
        x = F.glu(x)
        return x


class GRN(nn.Module):
    def __init__(
        self,
        input_size,
        hidden_size,
        output_size=None,
        context_hidden_size=None,
        dropout=0,
    ):
        super().__init__()

        self.layer_norm = MaybeLayerNorm(output_size, hidden_size, eps=1e-3)
        self.lin_a = nn.Linear(input_size, hidden_size)
        if context_hidden_size is not None:
            self.lin_c = nn.Linear(context_hidden_size, hidden_size, bias=False)
        self.lin_i = nn.Linear(hidden_size, hidden_size)
        self.glu = GLU(hidden_size, output_size if output_size else hidden_size)
        self.dropout = nn.Dropout(dropout)
        self.out_proj = nn.Linear(input_size, output_size) if output_size else None

    def forward(self, a: Tensor, c: Optional[Tensor] = None):
        x = self.lin_a(a)
        if c is not None:
            x = x + self.lin_c(c).unsqueeze(1)
        x = F.elu(x)
        x = self.lin_i(x)
        x = self.dropout(x)
        x = self.glu(x)
        y = a if not self.out_proj else self.out_proj(a)
        x = x + y
        x = self.layer_norm(x)
        return x

# %% ../../nbs/models.tft.ipynb 13
class TFTEmbedding(nn.Module):
    def __init__(
        self, hidden_size, stat_input_size, futr_input_size, hist_input_size, tgt_size
    ):
        super().__init__()
        # There are 4 types of input:
        # 1. Static continuous
        # 2. Temporal known a priori continuous
        # 3. Temporal observed continuous
        # 4. Temporal observed targets (time series obseved so far)

        self.hidden_size = hidden_size

        self.stat_input_size = stat_input_size
        self.futr_input_size = futr_input_size
        self.hist_input_size = hist_input_size
        self.tgt_size = tgt_size

        # Instantiate Continuous Embeddings if size is not None
        for attr, size in [
            ("stat_exog_embedding", stat_input_size),
            ("futr_exog_embedding", futr_input_size),
            ("hist_exog_embedding", hist_input_size),
            ("tgt_embedding", tgt_size),
        ]:
            if size:
                vectors = nn.Parameter(torch.Tensor(size, hidden_size))
                bias = nn.Parameter(torch.zeros(size, hidden_size))
                torch.nn.init.xavier_normal_(vectors)
                setattr(self, attr + "_vectors", vectors)
                setattr(self, attr + "_bias", bias)
            else:
                setattr(self, attr + "_vectors", None)
                setattr(self, attr + "_bias", None)

    def _apply_embedding(
        self,
        cont: Optional[Tensor],
        cont_emb: Tensor,
        cont_bias: Tensor,
    ):

        if cont is not None:
            # the line below is equivalent to following einsums
            # e_cont = torch.einsum('btf,fh->bthf', cont, cont_emb)
            # e_cont = torch.einsum('bf,fh->bhf', cont, cont_emb)
            e_cont = torch.mul(cont.unsqueeze(-1), cont_emb)
            e_cont = e_cont + cont_bias
            return e_cont

        return None

    def forward(self, target_inp, stat_exog=None, futr_exog=None, hist_exog=None):
        # temporal/static categorical/continuous known/observed input
        # tries to get input, if fails returns None

        # Static inputs are expected to be equal for all timesteps
        # For memory efficiency there is no assert statement
        stat_exog = stat_exog[:, :] if stat_exog is not None else None

        s_inp = self._apply_embedding(
            cont=stat_exog,
            cont_emb=self.stat_exog_embedding_vectors,
            cont_bias=self.stat_exog_embedding_bias,
        )
        k_inp = self._apply_embedding(
            cont=futr_exog,
            cont_emb=self.futr_exog_embedding_vectors,
            cont_bias=self.futr_exog_embedding_bias,
        )
        o_inp = self._apply_embedding(
            cont=hist_exog,
            cont_emb=self.hist_exog_embedding_vectors,
            cont_bias=self.hist_exog_embedding_bias,
        )

        # Temporal observed targets
        # t_observed_tgt = torch.einsum('btf,fh->btfh',
        #                               target_inp, self.tgt_embedding_vectors)
        target_inp = torch.matmul(
            target_inp.unsqueeze(3).unsqueeze(4),
            self.tgt_embedding_vectors.unsqueeze(1),
        ).squeeze(3)
        target_inp = target_inp + self.tgt_embedding_bias

        return s_inp, k_inp, o_inp, target_inp


class VariableSelectionNetwork(nn.Module):
    def __init__(self, hidden_size, num_inputs, dropout):
        super().__init__()
        self.joint_grn = GRN(
            input_size=hidden_size * num_inputs,
            hidden_size=hidden_size,
            output_size=num_inputs,
            context_hidden_size=hidden_size,
        )
        self.var_grns = nn.ModuleList(
            [
                GRN(input_size=hidden_size, hidden_size=hidden_size, dropout=dropout)
                for _ in range(num_inputs)
            ]
        )

    def forward(self, x: Tensor, context: Optional[Tensor] = None):
        Xi = x.reshape(*x.shape[:-2], -1)
        grn_outputs = self.joint_grn(Xi, c=context)
        sparse_weights = F.softmax(grn_outputs, dim=-1)
        transformed_embed_list = [m(x[..., i, :]) for i, m in enumerate(self.var_grns)]
        transformed_embed = torch.stack(transformed_embed_list, dim=-1)
        # the line below performs batched matrix vector multiplication
        # for temporal features it's bthf,btf->bth
        # for static features it's bhf,bf->bh
        variable_ctx = torch.matmul(
            transformed_embed, sparse_weights.unsqueeze(-1)
        ).squeeze(-1)

        return variable_ctx, sparse_weights

# %% ../../nbs/models.tft.ipynb 15
class InterpretableMultiHeadAttention(nn.Module):
    def __init__(self, n_head, hidden_size, example_length, attn_dropout, dropout):
        super().__init__()
        self.n_head = n_head
        assert hidden_size % n_head == 0
        self.d_head = hidden_size // n_head
        self.qkv_linears = nn.Linear(
            hidden_size, (2 * self.n_head + 1) * self.d_head, bias=False
        )
        self.out_proj = nn.Linear(self.d_head, hidden_size, bias=False)

        self.attn_dropout = nn.Dropout(attn_dropout)
        self.out_dropout = nn.Dropout(dropout)
        self.scale = self.d_head**-0.5
        self.register_buffer(
            "_mask",
            torch.triu(
                torch.full((example_length, example_length), float("-inf")), 1
            ).unsqueeze(0),
        )

    def forward(
        self, x: Tensor, mask_future_timesteps: bool = True
    ) -> Tuple[Tensor, Tensor]:
        # [Batch,Time,MultiHead,AttDim] := [N,T,M,AD]
        bs, t, h_size = x.shape
        qkv = self.qkv_linears(x)
        q, k, v = qkv.split(
            (self.n_head * self.d_head, self.n_head * self.d_head, self.d_head), dim=-1
        )
        q = q.view(bs, t, self.n_head, self.d_head)
        k = k.view(bs, t, self.n_head, self.d_head)
        v = v.view(bs, t, self.d_head)

        # [N,T1,M,Ad] x [N,T2,M,Ad] -> [N,M,T1,T2]
        # attn_score = torch.einsum('bind,bjnd->bnij', q, k)
        attn_score = torch.matmul(q.permute((0, 2, 1, 3)), k.permute((0, 2, 3, 1)))
        attn_score.mul_(self.scale)

        if mask_future_timesteps:
            attn_score = attn_score + self._mask

        attn_prob = F.softmax(attn_score, dim=3)
        attn_prob = self.attn_dropout(attn_prob)

        # [N,M,T1,T2] x [N,M,T1,Ad] -> [N,M,T1,Ad]
        # attn_vec = torch.einsum('bnij,bjd->bnid', attn_prob, v)
        attn_vec = torch.matmul(attn_prob, v.unsqueeze(1))
        m_attn_vec = torch.mean(attn_vec, dim=1)
        out = self.out_proj(m_attn_vec)
        out = self.out_dropout(out)

        return out, attn_vec

# %% ../../nbs/models.tft.ipynb 18
class StaticCovariateEncoder(nn.Module):
    def __init__(self, hidden_size, num_static_vars, dropout):
        super().__init__()
        self.vsn = VariableSelectionNetwork(
            hidden_size=hidden_size, num_inputs=num_static_vars, dropout=dropout
        )
        self.context_grns = nn.ModuleList(
            [
                GRN(input_size=hidden_size, hidden_size=hidden_size, dropout=dropout)
                for _ in range(4)
            ]
        )

    def forward(self, x: Tensor) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        variable_ctx, sparse_weights = self.vsn(x)

        # Context vectors:
        # variable selection context
        # enrichment context
        # state_c context
        # state_h context
        cs, ce, ch, cc = tuple(m(variable_ctx) for m in self.context_grns)

        return cs, ce, ch, cc

# %% ../../nbs/models.tft.ipynb 20
class TemporalCovariateEncoder(nn.Module):
    def __init__(self, hidden_size, num_historic_vars, num_future_vars, dropout):
        super(TemporalCovariateEncoder, self).__init__()

        self.history_vsn = VariableSelectionNetwork(
            hidden_size=hidden_size, num_inputs=num_historic_vars, dropout=dropout
        )
        self.history_encoder = nn.LSTM(
            input_size=hidden_size, hidden_size=hidden_size, batch_first=True
        )

        self.future_vsn = VariableSelectionNetwork(
            hidden_size=hidden_size, num_inputs=num_future_vars, dropout=dropout
        )
        self.future_encoder = nn.LSTM(
            input_size=hidden_size, hidden_size=hidden_size, batch_first=True
        )

        # Shared Gated-Skip Connection
        self.input_gate = GLU(hidden_size, hidden_size)
        self.input_gate_ln = LayerNorm(hidden_size, eps=1e-3)

    def forward(self, historical_inputs, future_inputs, cs, ch, cc):
        # [N,X_in,L] -> [N,hidden_size,L]
        historical_features, _ = self.history_vsn(historical_inputs, cs)
        history, state = self.history_encoder(historical_features, (ch, cc))

        future_features, _ = self.future_vsn(future_inputs, cs)
        future, _ = self.future_encoder(future_features, state)
        # torch.cuda.synchronize() # this call gives prf boost for unknown reasons

        input_embedding = torch.cat([historical_features, future_features], dim=1)
        temporal_features = torch.cat([history, future], dim=1)
        temporal_features = self.input_gate(temporal_features)
        temporal_features = temporal_features + input_embedding
        temporal_features = self.input_gate_ln(temporal_features)
        return temporal_features

# %% ../../nbs/models.tft.ipynb 22
class TemporalFusionDecoder(nn.Module):
    def __init__(
        self, n_head, hidden_size, example_length, encoder_length, attn_dropout, dropout
    ):
        super(TemporalFusionDecoder, self).__init__()
        self.encoder_length = encoder_length

        # ------------- Encoder-Decoder Attention --------------#
        self.enrichment_grn = GRN(
            input_size=hidden_size,
            hidden_size=hidden_size,
            context_hidden_size=hidden_size,
            dropout=dropout,
        )
        self.attention = InterpretableMultiHeadAttention(
            n_head=n_head,
            hidden_size=hidden_size,
            example_length=example_length,
            attn_dropout=attn_dropout,
            dropout=dropout,
        )
        self.attention_gate = GLU(hidden_size, hidden_size)
        self.attention_ln = LayerNorm(normalized_shape=hidden_size, eps=1e-3)

        self.positionwise_grn = GRN(
            input_size=hidden_size, hidden_size=hidden_size, dropout=dropout
        )

        # ---------------------- Decoder -----------------------#
        self.decoder_gate = GLU(hidden_size, hidden_size)
        self.decoder_ln = LayerNorm(normalized_shape=hidden_size, eps=1e-3)

    def forward(self, temporal_features, ce):
        # ------------- Encoder-Decoder Attention --------------#
        # Static enrichment
        enriched = self.enrichment_grn(temporal_features, c=ce)

        # Temporal self attention
        x, _ = self.attention(enriched, mask_future_timesteps=True)

        # Don't compute historical quantiles
        x = x[:, self.encoder_length :, :]
        temporal_features = temporal_features[:, self.encoder_length :, :]
        enriched = enriched[:, self.encoder_length :, :]

        x = self.attention_gate(x)
        x = x + enriched
        x = self.attention_ln(x)

        # Position-wise feed-forward
        x = self.positionwise_grn(x)

        # ---------------------- Decoder ----------------------#
        # Final skip connection
        x = self.decoder_gate(x)
        x = x + temporal_features
        x = self.decoder_ln(x)

        return x

# %% ../../nbs/models.tft.ipynb 24
class TFT(BaseModel):
    """TFT

    The Temporal Fusion Transformer architecture (TFT) is an Sequence-to-Sequence
    model that combines static, historic and future available data to predict an
    univariate target. The method combines gating layers, an LSTM recurrent encoder,
    with and interpretable multi-head attention layer and a multi-step forecasting
    strategy decoder.

    **Parameters:**<br>
    `h`: int, Forecast horizon. <br>
    `input_size`: int, autorregresive inputs size, y=[1,2,3,4] input_size=2 -> y_[t-2:t]=[1,2].<br>
    `stat_exog_list`: str list, static continuous columns.<br>
    `hist_exog_list`: str list, historic continuous columns.<br>
    `futr_exog_list`: str list, future continuous columns.<br>
    `hidden_size`: int, units of embeddings and encoders.<br>
    `dropout`: float (0, 1), dropout of inputs VSNs.<br>
    `n_head`: int=4, number of attention heads in temporal fusion decoder.<br>
    `attn_dropout`: float (0, 1), dropout of fusion decoder's attention layer.<br>
    `shared_weights`: bool, If True, all blocks within each stack will share parameters. <br>
    `activation`: str, activation from ['ReLU', 'Softplus', 'Tanh', 'SELU', 'LeakyReLU', 'PReLU', 'Sigmoid'].<br>
    `loss`: PyTorch module, instantiated train loss class from [losses collection](https://nixtla.github.io/neuralforecast/losses.pytorch.html).<br>
    `valid_loss`: PyTorch module=`loss`, instantiated valid loss class from [losses collection](https://nixtla.github.io/neuralforecast/losses.pytorch.html).<br>
    `max_steps`: int=1000, maximum number of training steps.<br>
    `learning_rate`: float=1e-3, Learning rate between (0, 1).<br>
    `num_lr_decays`: int=-1, Number of learning rate decays, evenly distributed across max_steps.<br>
    `early_stop_patience_steps`: int=-1, Number of validation iterations before early stopping.<br>
    `val_check_steps`: int=100, Number of training steps between every validation loss check.<br>
    `batch_size`: int, number of different series in each batch.<br>
    `windows_batch_size`: int=None, windows sampled from rolled data, default uses all.<br>
    `inference_windows_batch_size`: int=-1, number of windows to sample in each inference batch, -1 uses all.<br>
    `start_padding_enabled`: bool=False, if True, the model will pad the time series with zeros at the beginning, by input size.<br>
    `valid_batch_size`: int=None, number of different series in each validation and test batch.<br>
    `step_size`: int=1, step size between each window of temporal data.<br>
    `scaler_type`: str='robust', type of scaler for temporal inputs normalization see [temporal scalers](https://nixtla.github.io/neuralforecast/common.scalers.html).<br>
    `random_seed`: int, random seed initialization for replicability.<br>
    `num_workers_loader`: int=os.cpu_count(), workers to be used by `TimeSeriesDataLoader`.<br>
    `drop_last_loader`: bool=False, if True `TimeSeriesDataLoader` drops last non-full batch.<br>
    `alias`: str, optional,  Custom name of the model.<br>
    `optimizer`: Subclass of 'torch.optim.Optimizer', optional, user specified optimizer instead of the default choice (Adam).<br>
    `optimizer_kwargs`: dict, optional, list of parameters used by the user specified `optimizer`.<br>
    `lr_scheduler`: Subclass of 'torch.optim.lr_scheduler.LRScheduler', optional, user specified lr_scheduler instead of the default choice (StepLR).<br>
    `lr_scheduler_kwargs`: dict, optional, list of parameters used by the user specified `lr_scheduler`.<br>
    `**trainer_kwargs`: int,  keyword trainer arguments inherited from [PyTorch Lighning's trainer](https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.trainer.trainer.Trainer.html?highlight=trainer).<br>

    **References:**<br>
    - [Bryan Lim, Sercan O. Arik, Nicolas Loeff, Tomas Pfister,
    "Temporal Fusion Transformers for interpretable multi-horizon time series forecasting"](https://www.sciencedirect.com/science/article/pii/S0169207021000637)
    """

    # Class attributes
    EXOGENOUS_FUTR = True
    EXOGENOUS_HIST = True
    EXOGENOUS_STAT = True
    MULTIVARIATE = False  # If the model produces multivariate forecasts (True) or univariate (False)
    RECURRENT = (
        False  # If the model produces forecasts recursively (True) or direct (False)
    )

    def __init__(
        self,
        h,
        input_size,
        tgt_size: int = 1,
        stat_exog_list=None,
        hist_exog_list=None,
        futr_exog_list=None,
        hidden_size: int = 128,
        n_head: int = 4,
        attn_dropout: float = 0.0,
        dropout: float = 0.1,
        loss=MAE(),
        valid_loss=None,
        max_steps: int = 1000,
        learning_rate: float = 1e-3,
        num_lr_decays: int = -1,
        early_stop_patience_steps: int = -1,
        val_check_steps: int = 100,
        batch_size: int = 32,
        valid_batch_size: Optional[int] = None,
        windows_batch_size: int = 1024,
        inference_windows_batch_size: int = 1024,
        start_padding_enabled=False,
        step_size: int = 1,
        scaler_type: str = "robust",
        num_workers_loader=0,
        drop_last_loader=False,
        random_seed: int = 1,
        optimizer=None,
        optimizer_kwargs=None,
        lr_scheduler=None,
        lr_scheduler_kwargs=None,
        **trainer_kwargs
    ):

        # Inherit BaseWindows class
        super(TFT, self).__init__(
            h=h,
            input_size=input_size,
            stat_exog_list=stat_exog_list,
            hist_exog_list=hist_exog_list,
            futr_exog_list=futr_exog_list,
            loss=loss,
            valid_loss=valid_loss,
            max_steps=max_steps,
            learning_rate=learning_rate,
            num_lr_decays=num_lr_decays,
            early_stop_patience_steps=early_stop_patience_steps,
            val_check_steps=val_check_steps,
            batch_size=batch_size,
            valid_batch_size=valid_batch_size,
            windows_batch_size=windows_batch_size,
            inference_windows_batch_size=inference_windows_batch_size,
            start_padding_enabled=start_padding_enabled,
            step_size=step_size,
            scaler_type=scaler_type,
            num_workers_loader=num_workers_loader,
            drop_last_loader=drop_last_loader,
            random_seed=random_seed,
            optimizer=optimizer,
            optimizer_kwargs=optimizer_kwargs,
            lr_scheduler=lr_scheduler,
            lr_scheduler_kwargs=lr_scheduler_kwargs,
            **trainer_kwargs
        )
        self.example_length = input_size + h

        futr_exog_size = max(self.futr_exog_size, 1)
        num_historic_vars = futr_exog_size + self.hist_exog_size + tgt_size

        # ------------------------------- Encoders -----------------------------#
        self.embedding = TFTEmbedding(
            hidden_size=hidden_size,
            stat_input_size=self.stat_exog_size,
            futr_input_size=futr_exog_size,
            hist_input_size=self.hist_exog_size,
            tgt_size=tgt_size,
        )

        self.static_encoder = StaticCovariateEncoder(
            hidden_size=hidden_size,
            num_static_vars=self.stat_exog_size,
            dropout=dropout,
        )

        self.temporal_encoder = TemporalCovariateEncoder(
            hidden_size=hidden_size,
            num_historic_vars=num_historic_vars,
            num_future_vars=futr_exog_size,
            dropout=dropout,
        )

        # ------------------------------ Decoders -----------------------------#
        self.temporal_fusion_decoder = TemporalFusionDecoder(
            n_head=n_head,
            hidden_size=hidden_size,
            example_length=self.example_length,
            encoder_length=self.input_size,
            attn_dropout=attn_dropout,
            dropout=dropout,
        )

        # Adapter with Loss dependent dimensions
        self.output_adapter = nn.Linear(
            in_features=hidden_size, out_features=self.loss.outputsize_multiplier
        )

    def forward(self, windows_batch):

        # Parsiw windows_batch
        y_insample = windows_batch["insample_y"]
        futr_exog = windows_batch["futr_exog"]
        hist_exog = windows_batch["hist_exog"]
        stat_exog = windows_batch["stat_exog"]

        if futr_exog is None:
            futr_exog = y_insample[:, [-1]]
            futr_exog = futr_exog.repeat(1, self.example_length, 1)

        s_inp, k_inp, o_inp, t_observed_tgt = self.embedding(
            target_inp=y_insample,
            hist_exog=hist_exog,
            futr_exog=futr_exog,
            stat_exog=stat_exog,
        )

        # -------------------------------- Inputs ------------------------------#
        # Static context
        if s_inp is not None:
            cs, ce, ch, cc = self.static_encoder(s_inp)
            ch, cc = ch.unsqueeze(0), cc.unsqueeze(0)  # LSTM initial states
        else:
            # If None add zeros
            batch_size, example_length, target_size, hidden_size = t_observed_tgt.shape
            cs = torch.zeros(size=(batch_size, hidden_size), device=y_insample.device)
            ce = torch.zeros(size=(batch_size, hidden_size), device=y_insample.device)
            ch = torch.zeros(
                size=(1, batch_size, hidden_size), device=y_insample.device
            )
            cc = torch.zeros(
                size=(1, batch_size, hidden_size), device=y_insample.device
            )

        # Historical inputs
        _historical_inputs = [
            k_inp[:, : self.input_size, :],
            t_observed_tgt[:, : self.input_size, :],
        ]
        if o_inp is not None:
            _historical_inputs.insert(0, o_inp[:, : self.input_size, :])
        historical_inputs = torch.cat(_historical_inputs, dim=-2)

        # Future inputs
        future_inputs = k_inp[:, self.input_size :]

        # ---------------------------- Encode/Decode ---------------------------#
        # Embeddings + VSN + LSTM encoders
        temporal_features = self.temporal_encoder(
            historical_inputs=historical_inputs,
            future_inputs=future_inputs,
            cs=cs,
            ch=ch,
            cc=cc,
        )

        # Static enrichment, Attention and decoders
        temporal_features = self.temporal_fusion_decoder(
            temporal_features=temporal_features, ce=ce
        )

        # Adapt output to loss
        y_hat = self.output_adapter(temporal_features)

        return y_hat
