import torch
import torch.nn as nn
import numpy as np

from utils.spatial_transform import SpatialTransformer


class Bottleneck(nn.Module):
    def __init__(self, image_size):
        super(Bottleneck, self).__init__()
        self.image_size = image_size
        self.ndims = len(image_size)

        enc_nf = [16, 32, 32, 32, 64]
        dec_nf = [64, 32, 32, 32, 32, 16, 16, 8, 2]
        self.unet = Unet2(inshape=image_size, infeats=2, nb_features=[enc_nf, dec_nf])

        C = enc_nf[-1]
        W = image_size[0] // (2 ** len(enc_nf))
        H = image_size[1] // (2 ** len(enc_nf))
        self.input_size = C * W * H
        self.hidden_size = C * W * H
        self.lstm = nn.LSTM(input_size=self.input_size, hidden_size=self.hidden_size, batch_first=False)

        self.spatial_transformer = SpatialTransformer(size=image_size)

    def forward(self, images, labels=None):
        # shape of imgs/lbs: (40, bs, 1, 256, 256)

        # shape of encoder_out: (T, bs, C, W, H)
        X, X_history = [], []
        for src, trg in zip(images[:-1], images[1:]):
            x, x_history = self.unet(torch.cat([src, trg], dim=1), 'encode')
            X.append(x.unsqueeze(0))
            X_history.append(x_history)
        encoder_out = torch.cat(X, dim=0)

        T, bs, C, W, H = encoder_out.shape
        assert bs == 1
        assert T == 39

        # shape of lstm_out: (T, bs, C, W, H)
        device = 'cuda' if images.is_cuda else 'cpu'
        h_0 = torch.randn(1, bs, self.hidden_size).to(device)
        c_0 = torch.randn(1, bs, self.hidden_size).to(device)
        lstm_out, (h_n, c_n) = self.lstm(encoder_out.view(T, bs, -1), (h_0, c_0))
        lstm_out = lstm_out.view(T, bs, C, W, H)

        # shape of decoder_out: (T, bs, 2, 256, 256)
        Y = [self.unet(lstm_out[i], 'decode', X_history[i]).unsqueeze(0) for i in range(T)]
        flow = torch.cat(Y, dim=0)

        # shape of moved_images = (39, bs, 1, 256, 256)
        moved_images = torch.cat(
            [self.spatial_transformer(src, flow).unsqueeze(0) for src, flow in zip(images[:-1], flow)], dim=0)

        if labels is not None:
            moved_labels = torch.cat(
                [self.spatial_transformer(src, flow).unsqueeze(0) for src, flow in zip(labels[:-1], flow)], dim=0)
            return [moved_images, moved_labels, flow]
        else:
            return [moved_images, flow]


class Unet2(nn.Module):
    def __init__(self,
                 inshape=None,
                 infeats=None,
                 nb_features=None,
                 nb_levels=None,
                 max_pool=2,
                 feat_mult=1,
                 nb_conv_per_level=1,
                 half_res=False):

        super().__init__()

        ndims = len(inshape)
        assert ndims in [1, 2, 3], 'ndims should be one of 1, 2, or 3. found: %d' % ndims
        self.half_res = half_res

        # default encoder and decoder layer features if nothing provided
        if nb_features is None:
            enc_nf = [16, 32, 32, 32]
            dec_nf = [32, 32, 32, 32, 32, 16, 16]
            nb_features = [enc_nf, dec_nf]

        # build feature list automatically
        if isinstance(nb_features, int):
            if nb_levels is None:
                raise ValueError('must provide unet nb_levels if nb_features is an integer')
            feats = np.round(nb_features * feat_mult ** np.arange(nb_levels)).astype(int)
            nb_features = [
                np.repeat(feats[:-1], nb_conv_per_level),
                np.repeat(np.flip(feats), nb_conv_per_level)
            ]
        elif nb_levels is not None:
            raise ValueError('cannot use nb_levels if nb_features is not an integer')

        # extract any surplus (full resolution) decoder convolutions
        enc_nf, dec_nf = nb_features
        nb_dec_convs = len(enc_nf)
        final_convs = dec_nf[nb_dec_convs:]
        dec_nf = dec_nf[:nb_dec_convs]
        self.nb_levels = int(nb_dec_convs / nb_conv_per_level) + 1

        if isinstance(max_pool, int):
            max_pool = [max_pool] * self.nb_levels

        # cache downsampling / upsampling operations
        MaxPooling = getattr(nn, 'MaxPool%dd' % ndims)
        self.pooling = [MaxPooling(s) for s in max_pool]
        self.upsampling = [nn.Upsample(scale_factor=s, mode='nearest') for s in max_pool]

        # configure encoder (down-sampling path)
        prev_nf = infeats
        encoder_nfs = [prev_nf]
        self.encoder = nn.ModuleList()
        for level in range(self.nb_levels - 1):
            convs = nn.ModuleList()
            for conv in range(nb_conv_per_level):
                nf = enc_nf[level * nb_conv_per_level + conv]
                convs.append(ConvBlock(ndims, prev_nf, nf))
                prev_nf = nf
            self.encoder.append(convs)
            encoder_nfs.append(prev_nf)

        # configure decoder (up-sampling path)
        encoder_nfs = np.flip(encoder_nfs)
        self.decoder = nn.ModuleList()
        for level in range(self.nb_levels - 1):
            convs = nn.ModuleList()
            for conv in range(nb_conv_per_level):
                nf = dec_nf[level * nb_conv_per_level + conv]
                convs.append(ConvBlock(ndims, prev_nf, nf))
                prev_nf = nf
            self.decoder.append(convs)
            if not half_res or level < (self.nb_levels - 2):
                prev_nf += encoder_nfs[level]

        # now we take care of any remaining convolutions
        self.remaining = nn.ModuleList()
        for num, nf in enumerate(final_convs):
            self.remaining.append(ConvBlock(ndims, prev_nf, nf))
            prev_nf = nf

        # cache final number of features
        self.final_nf = prev_nf

    def forward(self, x, task, x_history_=None):

        # encoder forward pass
        if task == 'encode':
            x_history = [x]
            for level, convs in enumerate(self.encoder):
                for conv in convs:
                    x = conv(x)
                x_history.append(x)
                x = self.pooling[level](x)

            return x, x_history

        # decoder forward pass with upsampling and concatenation
        elif task == 'decode':
            x_history = x_history_
            assert x_history is not None, "x_history_ is None."
            for level, convs in enumerate(self.decoder):
                for conv in convs:
                    x = conv(x)
                if not self.half_res or level < (self.nb_levels - 2):
                    x = self.upsampling[level](x)
                    x = torch.cat([x, x_history.pop()], dim=1)

            # remaining convs at full resolution
            for conv in self.remaining:
                x = conv(x)

            return x


class ConvBlock(nn.Module):
    """
    Specific convolutional block followed by leakyrelu for unet.
    """

    def __init__(self, ndims, in_channels, out_channels, stride=1):
        super().__init__()

        Conv = getattr(nn, 'Conv%dd' % ndims)
        self.main = Conv(in_channels, out_channels, 3, stride, 1)
        self.activation = nn.LeakyReLU(0.2)

    def forward(self, x):
        out = self.main(x)
        out = self.activation(out)

        return out
