from matplotlib import pyplot as plt
import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from torch.nn.init import trunc_normal_

from basicsr.utils.registry import ARCH_REGISTRY


def get_local_weights(residual, ksize, padding):
    pad = padding
    residual_pad = F.pad(residual, pad=[pad, pad, pad, pad], mode='reflect')
    unfolded_residual = residual_pad.unfold(2, ksize, 3).unfold(3, ksize, 3)
    pixel_level_weight = torch.var(unfolded_residual, dim=(-1, -2), unbiased=True, keepdim=True).squeeze(-1).squeeze(-1)

    return pixel_level_weight


class UpsampleOneStep(nn.Sequential):
    """UpsampleOneStep module (the difference with Upsample is that it always only has 1conv + 1pixelshuffle)
       Used in lightweight SR to save parameters.
    Args:
        scale (int): Scale factor. Supported scales: 2^n and 3.
        num_feat (int): Channel number of intermediate features.
    """

    def __init__(self, scale, num_feat, num_out_ch, input_resolution=None):
        self.num_feat = num_feat
        self.input_resolution = input_resolution
        m = []
        m.append(nn.Conv2d(num_feat, (scale ** 2) * num_out_ch, 3, 1, 1))
        m.append(nn.PixelShuffle(scale))
        super(UpsampleOneStep, self).__init__(*m)

    def flops(self):
        h, w = self.input_resolution
        flops = h * w * self.num_feat * 3 * 9
        return flops


class PixelShuffleDirect(nn.Module):
    def __init__(self, scale, num_feat, num_out_ch):
        super(PixelShuffleDirect, self).__init__()
        self.upsampleOneStep = UpsampleOneStep(scale, num_feat, num_out_ch, input_resolution=None)

    def forward(self, x):
        return self.upsampleOneStep(x)


class BSConvU(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1,
                 dilation=1, bias=True, padding_mode="zeros", with_ln=False, bn_kwargs=None):
        super().__init__()

        # pointwise
        self.pw = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=(1, 1),
            stride=1,
            padding=0,
            dilation=1,
            groups=1,
            bias=False,
        )

        # depthwise
        self.dw = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=out_channels,
            bias=bias,
            padding_mode='reflect',
        )

    def forward(self, fea):
        fea = self.pw(fea)
        fea = self.dw(fea)
        return fea


class BSConvU2(nn.Module):

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size=3,
                 stride=1,
                 padding=1,
                 dilation=1,
                 bias=True,
                 padding_mode="zeros"):
        super().__init__()

        # pointwise
        self.pw = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=(1, 1),
            stride=1,
            padding=0,
            dilation=1,
            groups=1,
            bias=False,
        )

        # depthwise
        self.dw = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=out_channels,
            bias=bias,
            padding_mode=padding_mode,
        )

        self.rep1x1 = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=(1, 1),
            stride=1,
            padding=0,
            dilation=dilation,
            groups=out_channels,
            bias=bias,
            padding_mode=padding_mode,
        )

    def forward(self, fea):
        # shortcut = fea.clone()
        # fea = self.project_in(fea)
        # fea1, fea2 = self.dwconv(fea).chunk(2, dim=1)
        x = fea
        fea = self.pw(fea)  # + self.rep1x1(fea) #self.pw(fea) + fea
        fea = self.dw(fea) + x + self.rep1x1(fea)
        # fea = self.dwd(fea) + fea + self.rep1x1(fea)
        return fea

    def forward(self, fea):
        # shortcut = fea.clone()
        # fea = self.project_in(fea)
        # fea1, fea2 = self.dwconv(fea).chunk(2, dim=1)
        x = fea
        fea = self.pw(fea)  # + self.rep1x1(fea) #self.pw(fea) + fea
        fea = self.dw(fea) + x + self.rep1x1(fea)
        # fea = self.dwd(fea) + fea + self.rep1x1(fea)
        return fea


def stdv_channels(F):
    assert (F.dim() == 4)
    F_mean = mean_channels(F)
    F_variance = (F - F_mean).pow(2).sum(3, keepdim=True).sum(2, keepdim=True) / (F.size(2) * F.size(3))
    return F_variance.pow(0.5)


def mean_channels(F):
    assert (F.dim() == 4)
    spatial_sum = F.sum(3, keepdim=True).sum(2, keepdim=True)
    return spatial_sum / (F.size(2) * F.size(3))


class CCALayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(CCALayer, self).__init__()

        self.contrast = stdv_channels
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv_du = nn.Sequential(
            nn.Conv2d(channel, channel // reduction, 1, padding=0, bias=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(channel // reduction, channel, 1, padding=0, bias=True),
            nn.Sigmoid()
        )

    def forward(self, x):
        y = self.contrast(x) + self.avg_pool(x)
        y = self.conv_du(y)
        return x * y


# class ECCA(nn.Module):
#     def __init__(self, c_dim, reduction):
#         super().__init__()
#         self.body = nn.Sequential(nn.Conv2d(c_dim, c_dim, (1, 1), padding='same'),
#                                   nn.GELU(),
#                                   CCALayer(c_dim, reduction),
#                                   nn.Conv2d(c_dim, c_dim, (3, 3), padding='same', groups=c_dim))
#
#     def forward(self, x):
#         ca_x = self.body(x)
#         ca_x += x
#         return ca_x

# class ECCA(nn.Module):
#     def __init__(self, c_dim, reduction):
#         super().__init__()
#         self.conv1 = nn.Conv2d(c_dim , c_dim , 1, 1,1)
#         self.conv = nn.Conv2d(c_dim , c_dim , 1, 1)
#         self.pwc = nn.Conv2d(c_dim, c_dim * 2, 1, 1 , groups=c_dim)
#         self.GELU = nn.GELU()
#         self.convd = nn.Conv2d(c_dim , c_dim ,3,1 ,1, groups=c_dim, dilation=2)
#         self.cca = CCALayer(c_dim , c_dim )
#
#     def forward(self, x):
#         x1 = self.pwc(x)
#         x1 = self.GELU(x1)
#         x11,x12 = x1.chunk(2, dim=1)
#         x1_l = x11
#         x1_r = x12
#         # x11 = x1
#         # x12 = x1
#         x11 = self.cca(x11)
#         x12 = self.conv1(x12)
#         x12 = self.GELU(x12)
#         x12 = self.convd(x12)
#         x11 = x11 + x1_l
#         x12 = x12 + x1_r
#         x = x + x11 + x12
#         return x


# class ECCA(nn.Module):
#     def __init__(self, c_dim, reduction=16):
#         super().__init__()
#         self.conv1 = nn.Conv2d(c_dim//2, c_dim//2, 1, 1, 1)
#         self.dwconv = nn.Conv2d(c_dim, c_dim//2, 3, 1, 1, groups=c_dim//2)
#         self.GELU = nn.GELU()
#         #self.Sigmoid = nn.Sigmoid()
#         self.convd = nn.Conv2d(c_dim//2, c_dim//2,3,1 ,1, groups=c_dim//2, dilation=2)   # 5,1,5,3
#         self.cca = CCALayer(c_dim//2, c_dim//2)
#         self.conv2 = nn.Conv2d(c_dim//2, c_dim,1,1,)
#
#     def forward(self, x):
#         x1 = self.dwconv(x)
#         x1 = self.GELU(x1)
#         x11 = x1
#         x12 = x1
#         x11 = self.cca(x11)
#         x12 = self.conv1(x12)
#         x12 = self.GELU(x12)
#         x12 = self.convd(x12)
#         x11 = x11 + x1
#         x12 = x12 + x1
#         x11 = self.conv2(x11)
#         x12 = self.conv2(x12)
#         x = x + x11 + x12
#         return x

class ECCA(nn.Module):
    def __init__(self, c_dim, reduction=16):
        super().__init__()
        self.conv1 = nn.Conv2d(c_dim, c_dim, 1, 1, 0, groups=c_dim)
        self.dwconv = nn.Conv2d(c_dim, c_dim, 3, 1, 1, groups=c_dim)  # 3,1,1
        self.GELU = nn.GELU()
        # self.Sigmoid = nn.Sigmoid()
        self.convd = BSConvU(c_dim, c_dim)  # nn.Conv2d(c_dim, c_dim,3,1 ,1, groups=c_dim, dilation=2)   # 5,1,5,3
        self.cca = CCALayer(c_dim, c_dim)

    # self.conv2 = nn.Conv2d(c_dim, c_dim,1,1,)

    def forward(self, x):
        x1 = self.dwconv(x)
        x1 = self.GELU(x1)
        x11 = x1
        x12 = x1
        x11 = self.cca(x11)
        x12 = self.conv1(x12)
        x12 = self.GELU(x12)
        x12 = self.convd(x12)
        x11 = x11 + x1
        x12 = x12 + x1
        x = x + x11 + x12
        return x


# class ECCA(nn.Module):
#     def __init__(self, c_dim, reduction):
#         super().__init__()
#         self.body = nn.Sequential(nn.Conv2d(c_dim, c_dim, (1, 1), padding='same'),
#                                   nn.GELU(),
#                                   CCALayer(c_dim, reduction),
#                                   nn.Conv2d(c_dim, c_dim, (3, 3), padding='same', groups=c_dim))
#
#     def forward(self, x):
#         ca_x = self.body(x)
#         ca_x += x
#         return ca_x


class ESA(nn.Module):
    def __init__(self, num_feat, conv=nn.Conv2d):
        super(ESA, self).__init__()
        f = num_feat // 4
        self.conv1 = nn.Conv2d(num_feat, f, 1)
        self.conv_f = nn.Conv2d(f, f, 1)
        self.conv2_0 = conv(f, f, 3, 2, 1, padding_mode='reflect')
        self.conv2_1 = conv(f, f, 3, 2, 1, padding_mode='reflect')
        self.conv2_2 = conv(f, f, 3, 2, 1, padding_mode='reflect')
        self.conv2_3 = conv(f, f, 3, 2, 1, padding_mode='reflect')

        self.maxPooling_0 = nn.MaxPool2d(kernel_size=3, stride=3, padding=1)
        self.maxPooling_1 = nn.MaxPool2d(kernel_size=5, stride=3)
        self.maxPooling_2 = nn.MaxPool2d(kernel_size=7, stride=3, padding=1)
        self.maxPooling_3 = nn.MaxPool2d(kernel_size=9, stride=3, padding=1)
        self.conv_max_0 = BSConvU2(f, f, kernel_size=3)
        self.conv_max_1 = BSConvU2(f, f, kernel_size=3)
        self.conv_max_2 = BSConvU2(f, f, kernel_size=3)
        self.conv_max_3 = BSConvU2(f, f, kernel_size=3)
        # self.var_3 = get_local_weights

        self.conv3_0 = BSConvU(f, f, kernel_size=3)
        self.conv3_1 = BSConvU(f, f, kernel_size=3)
        self.conv3_2 = BSConvU(f, f, kernel_size=3)
        self.conv3_3 = BSConvU(f, f, kernel_size=3)
        self.conv4 = nn.Conv2d(f, num_feat, 1)
        self.sigmoid = nn.Sigmoid()
        self.GELU = nn.GELU()
        # self.norm = nn.BatchNorm2d(num_feat)
        # self.seita = nn.Parameter(torch.normal(mean=0.5, std=0.01, size=(1, 1, 1)))
        # self.keci = nn.Parameter(torch.normal(mean=0.5, std=0.01, size=(1, 1, 1)))
        #
        # self.alpha = nn.Parameter(torch.normal(mean=0.25, std=0.01, size=(1,1,1)))
        # self.beta = nn.Parameter(torch.normal(mean=0.25, std=0.01, size=(1,1,1)))
        # self.gama = nn.Parameter(torch.normal(mean=0.25, std=0.01, size=(1,1,1)))
        # self.omega = nn.Parameter(torch.normal(mean=0.25, std=0.01, size=(1,1,1)))

    def forward(self, input):
        c1_ = self.conv1(input)  # channel squeeze
        temp = self.conv2_0(c1_)
        c1_0 = self.maxPooling_0(temp)  # strided conv 3
        c1_1 = self.maxPooling_1(self.conv2_1(c1_))  # strided conv 5
        c1_2 = self.maxPooling_2(self.conv2_2(c1_))  # strided conv 7
        c1_3 = self.maxPooling_3(self.conv2_3(c1_))
        # c1_3 = self.var_3(self.conv2_3(c1_), 7, padding=1)  # strided local-var 7

        v_range_0 = self.conv3_0(self.GELU(self.conv_max_0(c1_0)))
        v_range_1 = self.conv3_1(self.GELU(self.conv_max_1(c1_1)))
        v_range_2 = self.conv3_2(
            self.GELU(self.conv_max_2(c1_2)))  # v_range_2 = self.conv3_2(self.GELU(self.conv_max_2(c1_2 + c1_3)))
        v_range_3 = self.conv3_3(self.GELU(self.conv_max_3(c1_3)))  # 原来写的是max_2

        c3_0 = F.interpolate(v_range_0, (input.size(2), input.size(3)), mode='bilinear', align_corners=False)
        c3_1 = F.interpolate(v_range_1, (input.size(2), input.size(3)), mode='bilinear', align_corners=False)
        c3_2 = F.interpolate(v_range_2, (input.size(2), input.size(3)), mode='bilinear', align_corners=False)
        c3_3 = F.interpolate(v_range_3, (input.size(2), input.size(3)), mode='bilinear', align_corners=False)

        cf = self.conv_f(c1_)
        c4 = self.conv4((
                                    c3_0 + c3_1 + c3_2 + cf + c3_3))  # self.conv4((c3_0 + c3_1 + c3_2 + cf + c3_3)) c4 = self.conv4((c3_0 + c3_1 + c3_2 + cf + c3_3 ))
        m = self.sigmoid(c4)

        return input * m


class MDSA(nn.Module):
    def __init__(self, c_dim, conv):
        super().__init__()
        self.body = nn.Sequential(ESA(c_dim, conv))

    def forward(self, x):
        sa_x = self.body(x)
        sa_x += x
        return sa_x


class BSConvU_rep(nn.Module):

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size=3,
                 stride=1,
                 padding=1,
                 dilation=1,
                 bias=True,
                 padding_mode="zeros"):
        super().__init__()

        # pointwise
        self.pw = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=(1, 1),
            stride=1,
            padding=0,
            dilation=1,
            groups=1,
            bias=False,
        )

        # depthwise
        self.dw = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=out_channels,
            bias=bias,
            padding_mode=padding_mode,
        )
        self.dwd = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            dilation=1,
            groups=out_channels,
            bias=bias,
            padding_mode=padding_mode,
        )

        self.rep1x1 = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=(1, 1),
            stride=1,
            padding=0,
            dilation=dilation,
            groups=out_channels,
            bias=bias,
            padding_mode=padding_mode,
        )
        # self.project_in = nn.Conv2d(in_channels, out_channels * 3, kernel_size=1 , bias=bias)

        # self.dwconv = nn.Conv2d(out_channels * 2, out_channels * 2, kernel_size=3, stride=1, padding=1,
        # groups=out_channels * 2, bias=bias)
        # self.project_out = nn.Conv2d(out_channels, out_channels, kernel_size=1,bias=bias)

    def forward(self, fea):
        # shortcut = fea.clone()
        # fea = self.project_in(fea)
        # fea1, fea2 = self.dwconv(fea).chunk(2, dim=1)
        # fea = self.pw(fea) + fea  # + self.rep1x1(fea) #self.pw(fea) + fea
        # fea = self.dw(fea) + fea + self.rep1x1(fea)
        # fea = fea + self.rep1x1(fea)   #self.dw(fea) +
        # return fea
        x = fea  # 记得拿回
        fea = self.pw(fea) + fea  # + self.rep1x1(fea) #self.pw(fea) + fea
        fea = self.dw(fea) + fea + self.rep1x1(fea)
        # fea = self.dwd(fea) + fea + self.rep1x1(fea)  #记得消去
        return fea


# class Dconv(nn.Module):
#     def __init__(self, in_dim, out_dim, kernel_size, padding=1):
#         super().__init__()
#         self.dim = in_dim // 3
#         self.in_dim = in_dim
#         self.out_dim = out_dim
#         self.conv1 = nn.Conv2d(in_dim, in_dim, (1, 1))
#         self.pw = nn.Conv2d(in_dim // 3, out_dim // 3,1 ,1)
#         self.conv2_1 = nn.Conv2d(in_dim // 3, out_dim // 3, (kernel_size, kernel_size), padding=padding,
#                                  groups=in_dim // 3)
#         self.conv2_1_D = nn.Conv2d(in_dim // 3, out_dim // 3, (kernel_size, kernel_size), padding=2,
#                                  groups=in_dim // 3, dilation=2)
#         #self.conv2_2 = nn.Conv2d(in_dim // 4, out_dim // 4, (3, 3), padding=padding ,
#                                  #groups=in_dim // 4)
#         #self.conv2_2_D = nn.Conv2d(in_dim // 4, out_dim // 4, (kernel_size, kernel_size), padding=3,
#                                   # groups=in_dim // 4, dilation=3)
#         self.conv2_3 = nn.Conv2d(in_dim // 3, out_dim // 3, (5, 5), padding=padding + 1,     # kernel_size, kernel_size * 2 - 1
#                                  groups=in_dim // 3)
#         self.conv2_4 = [
#             nn.Conv2d(in_dim // 3, out_dim // 3, (kernel_size, kernel_size), padding=padding, groups=in_dim // 3),
#             nn.Conv2d(in_dim // 3, out_dim // 3, (kernel_size, kernel_size), padding=padding, groups=in_dim // 3)]
#         self.conv2_4 = nn.Sequential(*self.conv2_4)
#         self.conv1x1_2 = nn.Conv2d(in_dim, in_dim, (1, 1))
#         self.act = nn.GELU()
#
#     def forward(self, input, flag=False):
#         out = self.conv1(input)
#         out = torch.chunk(out, 3, dim=1)
#         s1 = self.pw(self.conv2_1(out[0]))
#         s2 = self.pw(self.conv2_1_D(self.conv2_3(out[1] + s1 )))
#         #s3 = self.pw(self.conv2_2_D(self.conv2_3(out[2] + s1 )))
#         s3 = self.conv2_4(out[2] + s2 )   #s4 = self.conv2_4(out[3] + s3 )
#         out = torch.cat([s1, s2, s3], dim=1)
#         out = self.conv1x1_2(self.act(out))
#         return out

class EADB(nn.Module):
    def __init__(self, in_channels, conv=nn.Conv2d, padding=1):
        super(EADB, self).__init__()
        # kwargs = {'padding': 1}

        self.dc = self.distilled_channels = in_channels // 2
        self.rc = self.remaining_channels = in_channels

        self.c1_d = nn.Conv2d(in_channels, self.dc, 1)
        self.c1_r = BSConvU_rep(in_channels, self.rc, kernel_size=3, padding=padding)  # **kwargs
        self.c2_d = nn.Conv2d(self.remaining_channels, self.dc, 1)
        self.c2_r = BSConvU_rep(self.remaining_channels, self.rc, kernel_size=3, padding=padding)
        self.c3_d = nn.Conv2d(self.remaining_channels, self.dc, 1)
        self.c3_r = BSConvU_rep(self.remaining_channels, self.rc, kernel_size=3, padding=padding)
        self.atten = Attention(dim=in_channels)
        self.c4 = BSConvU(self.remaining_channels, self.dc, kernel_size=3)
        self.act = nn.GELU()
        # self.PAconv = nn.Conv2d(in_channels, in_channels, 1)
        ##add sconv to enhance res
        # self.sconv = SeparableConv(in_channels,5,deploy,dynamic,L)

        # self.conv1x1 = nn.Conv2d(4 * in_channels, in_channels, 1, 1, 0, bias=True, dilation=1, groups=1)
        # self.sigmoid = nn.Sigmoid()
        self.c5 = nn.Conv2d(self.dc * 4, in_channels, 1)
        self.esa = MDSA(in_channels, conv)
        self.cca = ECCA(in_channels, reduction=16)

    def forward(self, input):
        distilled_c1 = self.act(self.c1_d(input))
        r_c1 = (self.c1_r(input))
        r_c1 = self.act(r_c1)

        distilled_c2 = self.act(self.c2_d(r_c1))
        r_c2 = (self.c2_r(r_c1))
        r_c2 = self.act(r_c2)

        distilled_c3 = self.act(self.c3_d(r_c2))
        r_c3 = (self.c3_r(r_c2))
        r_c3 = self.act(r_c3)

        r_c4 = self.act(self.c4(r_c3))

        out = torch.cat([distilled_c1, distilled_c2, distilled_c3, r_c4], dim=1)
        out = self.c5(out)
        out = self.atten(out)
        # scale = self.sigmoid(out)#scale = self.sigmoid(self.PAconv(out))
        # res = self.conv1x1(torch.cat((input, res1, res2, res3), dim=1))
        # res = self.atten(res)
        # res = torch.mul(scale, res)#res = torch.mul(scale, res)
        # out_fuesd = res + out
        # out_fused = self.esa(out_fused + res)
        # print(out_fused.size())
        out_fused = self.esa(out)  # MDSA
        out_fused = self.cca(out_fused)  # ECCA
        # out_fused = self.cca(out)
        return out_fused + input  # , res


# 加入rep1x1效果更好
class Attention(nn.Module):

    def __init__(self, dim):
        super().__init__()
        self.pointwise = nn.Conv2d(dim, dim, 1)
        self.depthwise = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)  # 5,2,1
        self.depthwise_dilated = nn.Conv2d(dim, dim, 5, stride=1, padding=6, groups=dim, dilation=3)  # 5,1,16#padding=6
        self.rep1 = nn.Conv2d(dim, dim, 1, 1, 0, groups=dim)

    def forward(self, x):
        u = x.clone()
        attn = self.pointwise(x)
        attn = self.depthwise(attn) + self.rep1(attn)  # + attn
        attn = self.depthwise_dilated(attn) + self.rep1(attn)  # + attn
        return u * attn


# class Attention1(nn.Module):
#     def __init__(self, dim):
#         super().__init__()

#         self.proj_1 = nn.Conv2d(dim, dim, 1,groups=dim)
#         self.activation = nn.GELU()
#         self.spatial_gating_unit = Attention(dim)
#         self.proj_2 = nn.Conv2d(dim, dim, 1,groups=dim)

#     def forward(self, x):
#         shorcut = x.clone()
#         x = self.proj_1(x)
#         x = self.activation(x)
#         x = self.spatial_gating_unit(x)
#         x = self.proj_2(x)
#         x = x + shorcut
#         return x   #1为11

# class LSKA(nn.Module):
#     def __init__(self, dim, k_size=11):
#         super().__init__()

#         self.k_size = k_size

#         if k_size == 7:
#             self.conv0h = nn.Conv2d(dim, dim, kernel_size=(1, 3), stride=(1,1), padding=(0,(3-1)//2), groups=dim)
#             self.conv0v = nn.Conv2d(dim, dim, kernel_size=(3, 1), stride=(1,1), padding=((3-1)//2,0), groups=dim)
#             self.conv_spatial_h = nn.Conv2d(dim, dim, kernel_size=(1, 3), stride=(1,1), padding=(0,2), groups=dim, dilation=2)
#             self.conv_spatial_v = nn.Conv2d(dim, dim, kernel_size=(3, 1), stride=(1,1), padding=(2,0), groups=dim, dilation=2)
#         elif k_size == 11:
#             self.conv0h = nn.Conv2d(dim, dim, kernel_size=(1, 3), stride=(1,1), padding=(0,(3-1)//2), groups=dim)
#             self.conv0v = nn.Conv2d(dim, dim, kernel_size=(3, 1), stride=(1,1), padding=((3-1)//2,0), groups=dim)
#             self.conv_spatial_h = nn.Conv2d(dim, dim, kernel_size=(1, 5), stride=(1,1), padding=(0,4), groups=dim, dilation=2)
#             self.conv_spatial_v = nn.Conv2d(dim, dim, kernel_size=(5, 1), stride=(1,1), padding=(4,0), groups=dim, dilation=2)
#         elif k_size == 23:
#             self.conv0h = nn.Conv2d(dim, dim, kernel_size=(1, 5), stride=(1,1), padding=(0,(5-1)//2), groups=dim)
#             self.conv0v = nn.Conv2d(dim, dim, kernel_size=(5, 1), stride=(1,1), padding=((5-1)//2,0), groups=dim)
#             self.conv_spatial_h = nn.Conv2d(dim, dim, kernel_size=(1, 7), stride=(1,1), padding=(0,9), groups=dim, dilation=3)
#             self.conv_spatial_v = nn.Conv2d(dim, dim, kernel_size=(7, 1), stride=(1,1), padding=(9,0), groups=dim, dilation=3)
#         elif k_size == 35:
#             self.conv0h = nn.Conv2d(dim, dim, kernel_size=(1, 5), stride=(1,1), padding=(0,(5-1)//2), groups=dim)
#             self.conv0v = nn.Conv2d(dim, dim, kernel_size=(5, 1), stride=(1,1), padding=((5-1)//2,0), groups=dim)
#             self.conv_spatial_h = nn.Conv2d(dim, dim, kernel_size=(1, 11), stride=(1,1), padding=(0,15), groups=dim, dilation=3)
#             self.conv_spatial_v = nn.Conv2d(dim, dim, kernel_size=(11, 1), stride=(1,1), padding=(15,0), groups=dim, dilation=3)
#         elif k_size == 41:
#             self.conv0h = nn.Conv2d(dim, dim, kernel_size=(1, 5), stride=(1,1), padding=(0,(5-1)//2), groups=dim)
#             self.conv0v = nn.Conv2d(dim, dim, kernel_size=(5, 1), stride=(1,1), padding=((5-1)//2,0), groups=dim)
#             self.conv_spatial_h = nn.Conv2d(dim, dim, kernel_size=(1, 13), stride=(1,1), padding=(0,18), groups=dim, dilation=3)
#             self.conv_spatial_v = nn.Conv2d(dim, dim, kernel_size=(13, 1), stride=(1,1), padding=(18,0), groups=dim, dilation=3)
#         elif k_size == 53:
#             self.conv0h = nn.Conv2d(dim, dim, kernel_size=(1, 5), stride=(1,1), padding=(0,(5-1)//2), groups=dim)
#             self.conv0v = nn.Conv2d(dim, dim, kernel_size=(5, 1), stride=(1,1), padding=((5-1)//2,0), groups=dim)
#             self.conv_spatial_h = nn.Conv2d(dim, dim, kernel_size=(1, 17), stride=(1,1), padding=(0,24), groups=dim, dilation=3)
#             self.conv_spatial_v = nn.Conv2d(dim, dim, kernel_size=(17, 1), stride=(1,1), padding=(24,0), groups=dim, dilation=3)

#         self.conv1 = nn.Conv2d(dim, dim, 1)


#     def forward(self, x):
#         u = x.clone()
#         attn = self.conv0h(x)
#         attn = self.conv0v(attn)
#         attn = self.conv_spatial_h(attn)
#         attn = self.conv_spatial_v(attn)
#         attn = self.conv1(attn)
#         return u * attn


# class Attention2(nn.Module):
#     def __init__(self, dim, k_size=11):
#         super().__init__()

#         self.proj_1 = nn.Conv2d(dim, dim, 1,groups=dim)
#         self.activation = nn.GELU()
#         self.spatial_gating_unit = LSKA(dim, k_size)
#         self.proj_2 = nn.Conv2d(dim, dim, 1,groups=dim)

#     def forward(self, x):
#         shorcut = x.clone()
#         x = self.proj_1(x)
#         x = self.activation(x)
#         x = self.spatial_gating_unit(x)
#         x = self.proj_2(x)
#         x = x + shorcut
#         return x   #1为11

# class DFDB(nn.Module):
#     def __init__(self, in_channels, deploy=True, dynamic=True, L=None, style='DBB', res=True):
#         super(DFDB, self).__init__()
#
#         self.dc = self.distilled_channels = in_channels // 2
#         self.rc = self.remaining_channels = in_channels
#         self.res = res
#
#         self.act = torch.nn.SiLU()#(inplace=True)
#
#         self.c1_d = nn.Conv2d(in_channels, self.dc, 1)
#         self.c1_r = Dconv(self.rc, self.rc, kernel_size=3,padding=1)  #Unit(self.remaining_channels, self.rc, 3, deploy=deploy, dynamic=dynamic, L=L, style=style, res=res,
#         #                  nonlinear=self.act)
#         self.c2_d = nn.Conv2d(in_channels, self.dc, 1)
#         self.c2_r = Dconv(self.rc, self.rc, kernel_size=3, padding=1)     #Unit(self.remaining_channels, self.rc, 3, deploy=deploy, dynamic=dynamic, L=L, style=style, res=res,
#                          #nonlinear=self.act)
#         self.c3_d = nn.Conv2d(in_channels, self.dc, 1)
#         self.c3_r = nn.Conv2d(self.rc, self.rc, kernel_size=3, padding=1)
#         self.c4 = nn.Conv2d(self.remaining_channels, self.dc, 1, 1, 0, bias=True, dilation=1, groups=1)
#         self.c5 = nn.Conv2d(4 * self.dc, in_channels, 1, 1, 0, bias=True, dilation=1, groups=1)
#
#         if self.res:
#             self.PAconv = nn.Conv2d(in_channels, in_channels, 1)
#             ##add sconv to enhance res
#             # self.sconv = SeparableConv(in_channels,5,deploy,dynamic,L)
#
#             self.conv1x1 = nn.Conv2d(4 * in_channels, in_channels, 1, 1, 0, bias=True, dilation=1, groups=1)
#             self.sigmoid = nn.Sigmoid()
#
#        # self.esa = ESA(in_channels, nn.Conv2d)
#
#     def forward(self, input):
#         if self.res:
#             distilled_c1 = self.act(self.c1_d(input))
#             r_c1= self.c1_r(input)
#             res1 = r_c1
#             distilled_c2 = self.act(self.c2_d(r_c1))
#             r_c2= self.c2_r(r_c1)
#             res2=r_c2
#             distilled_c3 = self.act(self.c3_d(r_c2))
#             r_c3= self.c3_r(r_c2)
#             res3 = r_c3
#
#             r_c4 = self.c4(r_c3)
#
#             out = torch.cat((distilled_c1, distilled_c2, distilled_c3, r_c4), dim=1)
#
#             out_fused = self.c5(out)
#
#             # dfdb res
#             scale = self.sigmoid(self.PAconv(out_fused))
#             res = self.conv1x1(torch.cat((input, res1, res2, res3), dim=1))
#
#             res = torch.mul(scale, res)
#
#             out_fused = self.esa(out_fused + res)
#             # print(out_fused.size())
#             return out_fused, res
#
#         else:
#             distilled_c1 = self.act(self.c1_d(input))
#             r_c1 = self.c1_r(input)
#
#             distilled_c2 = self.act(self.c2_d(r_c1))
#             r_c2 = self.c2_r(r_c1)
#
#             distilled_c3 = self.act(self.c3_d(r_c2))
#             r_c3 = self.c3_r(r_c2)
#
#             r_c4 = self.c4(r_c3)
#
#             out = torch.cat((distilled_c1, distilled_c2, distilled_c3, r_c4), dim=1)
#             #out_fused = self.esa(self.c5(out))
#
#             return out


@ARCH_REGISTRY.register()
class OBLKHN(nn.Module):
    def __init__(self, num_in_ch=3, num_feat=56, num_block=6, num_out_ch=3, upscale=2,
                 rgb_mean=(0.4488, 0.4371, 0.4040), p=0.25):
        super(OBLKHN, self).__init__()
        kwargs = {'padding': 1}
        self.mean = torch.Tensor(rgb_mean).view(1, 3, 1, 1)
        # self.conv = BSConvU
        self.fea_conv = nn.Conv2d(num_in_ch, num_feat, kernel_size=3, stride=1, padding='same')

        self.B1 = EADB(in_channels=num_feat)  # , conv=self.conv)  #EADB DFDB
        self.B2 = EADB(in_channels=num_feat)  # , conv=self.conv)
        self.B3 = EADB(in_channels=num_feat)  # , conv=self.conv)
        self.B4 = EADB(in_channels=num_feat)  # , conv=self.conv)
        self.B5 = EADB(in_channels=num_feat)  # , conv=self.conv)
        self.B6 = EADB(in_channels=num_feat)  # , conv=self.conv)
        # self.B7 = EADB(in_channels=num_feat)#, conv=self.conv)
        # self.B8 = EADB(in_channels=num_feat)#, conv=self.conv)
        # self.B9 = EADB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        # self.B10 = EADB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)

        self.c1 = nn.Conv2d(num_feat * num_block, num_feat, 1)
        self.GELU = nn.GELU()

        self.c2 = BSConvU(num_feat, num_feat, kernel_size=3)

        # self.to_RGB = nn.Conv2d(num_feat, 3, 3, 1, 1)
        self.upsampler = PixelShuffleDirect(scale=upscale, num_feat=num_feat, num_out_ch=num_out_ch)

    def forward(self, input):
        self.mean = self.mean.type_as(input)
        input = input - self.mean
        # denosed_input = denosed_input - self.mean
        # SR
        out_fea = self.fea_conv(input)
        out_B1 = self.B1(out_fea)
        out_B2 = self.B2(out_B1)
        out_B3 = self.B3(out_B2)
        out_B4 = self.B4(out_B3)
        out_B5 = self.B5(out_B4)
        out_B6 = self.B6(out_B5)
        # out_B7 = self.B7(out_B6)
        # out_B8 = self.B8(out_B7)

        out = self.upsampler(self.c2(self.GELU(self.c1(torch.cat([out_B1, out_B2, out_B3, out_B4, out_B5, out_B6
                                                                  ], dim=1)))) + out_fea) + self.mean  # out_B5, out_B6
        return out

    def load_state_dict(self, state_dict, strict=False):
        own_state = self.state_dict()
        for name, param in state_dict.items():
            if name in own_state:
                if isinstance(param, nn.Parameter):
                    param = param.data
                try:
                    own_state[name].copy_(param)
                except Exception:
                    if name.find('tail') >= 0:
                        print('Replace pre-trained upsampler to new one...')
                    else:
                        raise RuntimeError('While copying the parameter named {}, '
                                           'whose dimensions in the model are {} and '
                                           'whose dimensions in the checkpoint are {}.'
                                           .format(name, own_state[name].size(), param.size()))
            elif strict:
                if name.find('tail') == -1:
                    raise KeyError('unexpected key "{}" in state_dict'
                                   .format(name))

        if strict:
            missing = set(own_state.keys()) - set(state_dict.keys())
            if len(missing) > 0:
                raise KeyError('missing keys in state_dict: "{}"'.format(missing))


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    input = torch.rand(2, 3, 64, 64).to(device)
    net = OBLKHN().to(device)

    output = net(input)
    print(output.size())
    model = OBLKHN()
    print(model)



# 保存模型为 .pt 文件
# torch.save(model.state_dict(), 'MDRN.pt')