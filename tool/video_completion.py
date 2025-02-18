import os
import sys
sys.path.append(os.path.abspath(os.path.join(__file__, '..', '..')))

import argparse
import os
import cv2
import glob
import copy
import numpy as np
import torch
import imutils
import imageio
from PIL import Image
import scipy.ndimage
from skimage.feature import canny
import torchvision.transforms.functional as F

from RAFT import utils
from RAFT import RAFT

import utils.region_fill as rf
from utils.Poisson_blend import Poisson_blend
from utils.Poisson_blend_img import Poisson_blend_img
from get_flowNN import get_flowNN
from get_flowNN_gradient import get_flowNN_gradient
from utils.common_utils import flow_edge
from spatial_inpaint import spatial_inpaint
from frame_inpaint import DeepFillv1
from edgeconnect.networks import EdgeGenerator_


def detectAndDescribe(image):
    # convert the image to grayscale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # check to see if we are using OpenCV 3.X
    if imutils.is_cv3(or_better=True):

        # detect and extract features from the image
        descriptor = cv2.xfeatures2d.SURF_create()
        (kps, features) = descriptor.detectAndCompute(image.astype(np.uint8), None)

        # orb feature is way faster
        # orb = cv2.ORB_create()
        # kp = orb.detect(gray, None)
        # (kps, features) = orb.compute(gray, kp)

    # otherwise, we are using OpenCV 2.4.X
    else:
        # detect keypoints in the image
        detector = cv2.FeatureDetector_create("SIFT")
        kps = detector.detect(gray)

        # extract features from the image
        extractor = cv2.DescriptorExtractor_create("SIFT")
        (kps, features) = extractor.compute(gray, kps)

    # convert the keypoints from KeyPoint objects to NumPy
    # arrays
    kps = np.float32([kp.pt for kp in kps])

    # return a tuple of keypoints and features
    return (kps, features)


def matchKeypoints(kpsA, kpsB, featuresA, featuresB, ratio=0.75, reprojThresh=4.0):
    # compute the raw matches and initialize the list of actual
    # matches
    matcher = cv2.DescriptorMatcher_create("BruteForce")
    rawMatches = matcher.knnMatch(featuresA, featuresB, 2)
    matches = []

    # loop over the raw matches
    for m in rawMatches:
        # ensure the distance is within a certain ratio of each
        # other (i.e. Lowe's ratio test)
        if len(m) == 2 and m[0].distance < m[1].distance * ratio:
            matches.append((m[0].trainIdx, m[0].queryIdx))

    # computing a homography requires at least 4 matches
    if len(matches) > 4:
        # construct the two sets of points
        ptsA = np.float32([kpsA[i] for (_, i) in matches])
        ptsB = np.float32([kpsB[i] for (i, _) in matches])

        # compute the homography between the two sets of points
        (H, status) = cv2.findHomography(ptsA, ptsB, cv2.RANSAC, reprojThresh)

        # return the matches along with the homograpy matrix
        # and status of each matched point
        return (matches, H, status)

    # otherwise, no homograpy could be computed
    return None


def homograpy(image1, image2):
    image1 = image1[0].permute(1, 2, 0).cpu().numpy()
    image2 = image2[0].permute(1, 2, 0).cpu().numpy()

    imgH, imgW, _ = image1.shape

    (kpsA, featuresA) = detectAndDescribe(image1)
    (kpsB, featuresB) = detectAndDescribe(image2)

    try:
        (_, H_BA, _) = matchKeypoints(kpsB, kpsA, featuresB, featuresA)
    except:
        H_BA = np.array([1.0,0,0,0,1.0,0,0,0,1.0]).reshape(3,3)

    NoneType = type(None)
    if type(H_BA) == NoneType:
        H_BA = np.array([1.0,0,0,0,1.0,0,0,0,1.0]).reshape(3,3)

    try:
        tmp = np.linalg.inv(H_BA)
    except:
        H_BA = np.array([1.0,0,0,0,1.0,0,0,0,1.0]).reshape(3,3)

    image2_registered = cv2.warpPerspective(image2, H_BA, (imgW, imgH))

    return image2_registered, H_BA


def to_tensor(img):
    img = Image.fromarray(img)
    img_t = F.to_tensor(img).float()
    return img_t


def infer(args, EdgeGenerator, device, flow_img_gray, edge, mask):

    # Add a pytorch dataloader
    flow_img_gray_tensor = to_tensor(flow_img_gray)[None, :, :].float().to(device)
    edge_tensor = to_tensor(edge)[None, :, :].float().to(device)
    mask_tensor = torch.from_numpy(mask.astype(np.float64))[None, None, :, :].float().to(device)

    # Complete the edges
    edges_masked = (edge_tensor * (1 - mask_tensor))
    images_masked = (flow_img_gray_tensor * (1 - mask_tensor)) + mask_tensor
    inputs = torch.cat((images_masked, edges_masked, mask_tensor), dim=1)
    with torch.no_grad():
        edges_completed = EdgeGenerator(inputs) # in: [grayscale(1) + edge(1) + mask(1)]
    edges_completed = edges_completed * mask_tensor + edge_tensor * (1 - mask_tensor)
    edge_completed = edges_completed[0, 0].data.cpu().numpy()
    edge_completed[edge_completed < 0.5] = 0
    edge_completed[edge_completed >= 0.5] = 1

    return edge_completed


def gradient_mask(mask):

    gradient_mask = np.logical_or.reduce((mask,
        np.concatenate((mask[1:, :], np.zeros((1, mask.shape[1]), dtype=bool)), axis=0),
        np.concatenate((mask[:, 1:], np.zeros((mask.shape[0], 1), dtype=bool)), axis=1)))

    return gradient_mask


def create_dir(dir):
    """Creates a directory if not exist.
    """
    if not os.path.exists(dir):
        os.makedirs(dir)


def initialize_RAFT(args):
    """Initializes the RAFT model.
    """
    model = torch.nn.DataParallel(RAFT(args))
    model.load_state_dict(torch.load(args.model))

    model = model.module
    model.to('cuda')
    model.eval()

    return model


def infer_flow(args, mode, filename, image1, image2, imgH, imgW, model, homography=False):

    if not homography:
        _, flow = model(image1, image2, iters=20, test_mode=True)
        flow = flow[0].permute(1, 2, 0).cpu().numpy()
    else:
        image2_reg, H_BA = homograpy(image1, image2)
        image2_reg = torch.tensor(image2_reg).permute(2, 0, 1)[None].float().to('cuda')
        _, flow = model(image1, image2_reg, iters=20, test_mode=True)
        flow = flow[0].permute(1, 2, 0).cpu().numpy()

        (fy, fx) = np.mgrid[0 : imgH, 0 : imgW].astype(np.float32)

        fxx = copy.deepcopy(fx) + flow[:, :, 0]
        fyy = copy.deepcopy(fy) + flow[:, :, 1]

        (fxxx, fyyy, fz) = np.linalg.inv(H_BA).dot(np.concatenate((fxx.reshape(1, -1),
                                                   fyy.reshape(1, -1),
                                                   np.ones_like(fyy).reshape(1, -1)), axis=0))
        fxxx, fyyy = fxxx / fz, fyyy / fz

        flow = np.concatenate((fxxx.reshape(imgH, imgW, 1) - fx.reshape(imgH, imgW, 1),
                               fyyy.reshape(imgH, imgW, 1) - fy.reshape(imgH, imgW, 1)), axis=2)

    Image.fromarray(utils.flow_viz.flow_to_image(flow)).save(os.path.join(args.outroot, 'flow', mode + '_png', filename + '.png'))
    utils.frame_utils.writeFlow(os.path.join(args.outroot, 'flow', mode + '_flo', filename + '.flo'), flow)

    return flow


def calculate_flow(args, model, video):
    """Calculates optical flow.
    """
    nFrame, _, imgH, imgW = video.shape
    FlowF = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    FlowB = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    FlowNLF = np.empty(((imgH, imgW, 2, 3, 0)), dtype=np.float32)
    FlowNLB = np.empty(((imgH, imgW, 2, 3, 0)), dtype=np.float32)
    # FlowNLF0 = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    # FlowNLF1 = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    # FlowNLF2 = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    # FlowNLB0 = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    # FlowNLB1 = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)
    # FlowNLB2 = np.empty(((imgH, imgW, 2, 0)), dtype=np.float32)

    if args.Nonlocal:
        mode_list = ['forward', 'backward', 'nonlocal_forward', 'nonlocal_backward']
    else:
        mode_list = ['forward', 'backward']

    for mode in mode_list:
        create_dir(os.path.join(args.outroot, 'flow', mode + '_flo'))
        create_dir(os.path.join(args.outroot, 'flow', mode + '_png'))

        with torch.no_grad():
            for i in range(nFrame):
                if mode == 'forward':
                    if i == nFrame - 1:
                        continue
                    # Flow i -> i + 1
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, i, i + 1), '\r', end='')
                    image1 = video[i, None]
                    image2 = video[i + 1, None]
                    flow = infer_flow(args, mode, '%05d'%i, image1, image2, imgH, imgW, model, homography=False)
                    FlowF = np.concatenate((FlowF, flow[..., None]), axis=-1)
                elif mode == 'backward':
                    if i == nFrame - 1:
                        continue
                    # Flow i + 1 -> i
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, i, i + 1), '\r', end='')
                    image1 = video[i + 1, None]
                    image2 = video[i, None]
                    flow = infer_flow(args, mode, '%05d'%i, image1, image2, imgH, imgW, model, homography=False)
                    FlowB = np.concatenate((FlowB, flow[..., None]), axis=-1)
                elif mode == 'nonlocal_forward':
                    # Flow i -> 0
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, i, 0), '\r', end='')
                    image1 = video[i, None]
                    image2 = video[0, None]
                    FlowNLF0 = infer_flow(args, mode, '%05d_00000'%i, image1, image2, imgH, imgW, model, homography=False)

                    # Flow i -> nFrame // 2
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, i, nFrame // 2), '\r', end='')
                    image1 = video[i, None]
                    image2 = video[nFrame // 2, None]
                    FlowNLF1 = infer_flow(args, mode, '%05d_00001'%i, image1, image2, imgH, imgW, model, homography=False)

                    # # Flow i -> nFrame - 1
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, i, nFrame - 1), '\r', end='')
                    image1 = video[i, None]
                    image2 = video[nFrame - 1, None]
                    FlowNLF2 = infer_flow(args, mode, '%05d_00002'%i, image1, image2, imgH, imgW, model, homography=False)

                    FlowNLF = np.concatenate((FlowNLF, np.stack((FlowNLF0, FlowNLF1, FlowNLF2), -1)[..., None]), axis=-1)

                elif mode == 'nonlocal_backward':
                    # Flow 0 -> i
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, 0, i), '\r', end='')
                    image1 = video[0, None]
                    image2 = video[i, None]
                    FlowNLB0 = infer_flow(args, mode, '%05d_00000'%i, image1, image2, imgH, imgW, model, homography=False)

                    # Flow nFrame // 2 -> i
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, nFrame // 2, i), '\r', end='')
                    image1 = video[nFrame // 2, None]
                    image2 = video[i, None]
                    FlowNLB1 = infer_flow(args, mode, '%05d_00001'%i, image1, image2, imgH, imgW, model, homography=False)

                    # # Flow nFrame - 1 -> i
                    print("Calculating {0} flow {1:2d} <---> {2:2d}".format(mode, nFrame - 1, i), '\r', end='')
                    image1 = video[nFrame - 1, None]
                    image2 = video[i, None]
                    FlowNLB2 = infer_flow(args, mode, '%05d_00002'%i, image1, image2, imgH, imgW, model, homography=False)

                    FlowNLB = np.concatenate((FlowNLB, np.stack((FlowNLB0, FlowNLB1, FlowNLB2), -1)[..., None]), axis=-1)

    return FlowF, FlowB, FlowNLF, FlowNLB


def extrapolation(args, video_ori, corrFlowF_ori, corrFlowB_ori, corrFlowNLF_ori, corrFlowNLB_ori):
    """Prepares the data for video extrapolation.
    """
    imgH, imgW, _, nFrame = video_ori.shape

    # Defines new FOV.
    imgH_extr = int(args.H_scale * imgH)
    imgW_extr = int(args.W_scale * imgW)
    H_start = int((imgH_extr - imgH) / 2)
    W_start = int((imgW_extr - imgW) / 2)

    # Generates the mask for missing region.
    flow_mask = np.ones(((imgH_extr, imgW_extr)), dtype=bool)
    flow_mask[H_start : H_start + imgH, W_start : W_start + imgW] = 0

    mask_dilated = gradient_mask(flow_mask)

    # Extrapolates the FOV for video.
    video = np.zeros(((imgH_extr, imgW_extr, 3, nFrame)), dtype=np.float32)
    video[H_start : H_start + imgH, W_start : W_start + imgW, :, :] = video_ori

    for i in range(nFrame):
        print("Preparing frame {0}".format(i), '\r', end='')
        video[:, :, :, i] = cv2.inpaint((video[:, :, :, i] * 255).astype(np.uint8), flow_mask.astype(np.uint8), 3, cv2.INPAINT_TELEA).astype(np.float32)  / 255.

    # Extrapolates the FOV for flow.
    corrFlowF = np.zeros(((imgH_extr, imgW_extr, 2, nFrame - 1)), dtype=np.float32)
    corrFlowB = np.zeros(((imgH_extr, imgW_extr, 2, nFrame - 1)), dtype=np.float32)

    corrFlowF[H_start : H_start + imgH, W_start : W_start + imgW] = corrFlowF_ori
    corrFlowB[H_start : H_start + imgH, W_start : W_start + imgW] = corrFlowB_ori

    if args.Nonlocal:
        corrFlowNLF = np.zeros(((imgH_extr, imgW_extr, 2, 3, nFrame)), dtype=np.float32)
        corrFlowNLB = np.zeros(((imgH_extr, imgW_extr, 2, 3, nFrame)), dtype=np.float32)

        corrFlowNLF[H_start : H_start + imgH, W_start : W_start + imgW] = corrFlowNLF_ori
        corrFlowNLB[H_start : H_start + imgH, W_start : W_start + imgW] = corrFlowNLB_ori
    else:
        corrFlowNLF = None
        corrFlowNLB = None

    return video, corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB, flow_mask, mask_dilated, (W_start, H_start), (W_start + imgW, H_start + imgH)


def complete_flow(args, corrFlow, flow_mask, mode, edge=None):
    """Completes flow.
    """
    if mode not in ['forward', 'backward', 'nonlocal_forward', 'nonlocal_backward']:
        raise NotImplementedError

    sh = corrFlow.shape
    imgH = sh[0]
    imgW = sh[1]
    nFrame = sh[-1]

    create_dir(os.path.join(args.outroot, 'flow_comp', mode + '_flo'))
    create_dir(os.path.join(args.outroot, 'flow_comp', mode + '_png'))

    compFlow = np.zeros(((sh)), dtype=np.float32)

    for i in range(nFrame):
        print("Completing {0} flow {1:2d} <---> {2:2d}".format(mode, i, i + 1), '\r', end='')
        flow = corrFlow[..., i]
        if mode == 'forward':
            flow_mask_img = flow_mask[:, :, i]
            flow_mask_gradient_img = gradient_mask(flow_mask_img)
        elif mode == 'backward':
            flow_mask_img = flow_mask[:, :, i + 1]
            flow_mask_gradient_img = gradient_mask(flow_mask_img)
        else: # nonlocal_backward
            assert edge == None

        if edge is not None:
            # imgH x (imgW - 1 + 1) x 2
            gradient_x = np.concatenate((np.diff(flow, axis=1), np.zeros((imgH, 1, 2), dtype=np.float32)), axis=1)
            # (imgH - 1 + 1) x imgW x 2
            gradient_y = np.concatenate((np.diff(flow, axis=0), np.zeros((1, imgW, 2), dtype=np.float32)), axis=0)

            # concatenate gradient_x and gradient_y
            gradient = np.concatenate((gradient_x, gradient_y), axis=2)

            # We can trust the gradient outside of flow_mask_gradient_img
            # We assume the gradient within flow_mask_gradient_img is 0.
            gradient[flow_mask_gradient_img, :] = 0

            # Complete the flow
            imgSrc_gy = gradient[:, :, 2 : 4]
            imgSrc_gy = imgSrc_gy[0 : imgH - 1, :, :]
            imgSrc_gx = gradient[:, :, 0 : 2]
            imgSrc_gx = imgSrc_gx[:, 0 : imgW - 1, :]
            compFlow[:, :, :, i] = Poisson_blend(flow, imgSrc_gx, imgSrc_gy, flow_mask_img, edge[:, :, i])

        else:
            if mode == 'forward' or mode == 'backward':
                flow[:, :, 0] = rf.regionfill(flow[:, :, 0], flow_mask_img)
                flow[:, :, 1] = rf.regionfill(flow[:, :, 1], flow_mask_img)
                compFlow[:, :, :, i] = flow
            elif mode == 'nonlocal_forward':
                flow[:, :, 0, 0] = rf.regionfill(flow[:, :, 0, 0], flow_mask[:, :, i])
                flow[:, :, 1, 0] = rf.regionfill(flow[:, :, 1, 0], flow_mask[:, :, i])
                flow[:, :, 0, 1] = rf.regionfill(flow[:, :, 0, 1], flow_mask[:, :, i])
                flow[:, :, 1, 1] = rf.regionfill(flow[:, :, 1, 1], flow_mask[:, :, i])
                flow[:, :, 0, 2] = rf.regionfill(flow[:, :, 0, 2], flow_mask[:, :, i])
                flow[:, :, 1, 2] = rf.regionfill(flow[:, :, 1, 2], flow_mask[:, :, i])
            else:
                flow[:, :, 0, 0] = rf.regionfill(flow[:, :, 0, 0], flow_mask[:, :, 0])
                flow[:, :, 1, 0] = rf.regionfill(flow[:, :, 1, 0], flow_mask[:, :, 0])
                flow[:, :, 0, 1] = rf.regionfill(flow[:, :, 0, 1], flow_mask[:, :, nFrame // 2])
                flow[:, :, 1, 1] = rf.regionfill(flow[:, :, 1, 1], flow_mask[:, :, nFrame // 2])
                flow[:, :, 0, 2] = rf.regionfill(flow[:, :, 0, 2], flow_mask[:, :, nFrame - 1])
                flow[:, :, 1, 2] = rf.regionfill(flow[:, :, 1, 2], flow_mask[:, :, nFrame - 1])
        # # Flow visualization.
        # flow_img = utils.flow_viz.flow_to_image(compFlow[:, :, :, i])
        # flow_img = Image.fromarray(flow_img)
        #
        # # Saves the flow and flow_img.
        # flow_img.save(os.path.join(args.outroot, 'flow_comp', mode + '_png', '%05d.png'%i))
        # utils.frame_utils.writeFlow(os.path.join(args.outroot, 'flow_comp', mode + '_flo', '%05d.flo'%i), compFlow[:, :, :, i])

    return compFlow


def edge_completion(args, EdgeGenerator, corrFlow, flow_mask, mode):
    """Calculate flow edge and complete it.
    """

    if mode not in ['forward', 'backward']:
        raise NotImplementedError

    imgH, imgW, _, nFrame = corrFlow.shape
    Edge = np.empty(((imgH, imgW, 0)), dtype=np.float32)

    for i in range(nFrame):
        print("Completing {0} flow edge {1:2d} <---> {2:2d}".format(mode, i, i + 1), '\r', end='')
        flow_mask_img = flow_mask[:, :, i] if mode == 'forward' else flow_mask[:, :, i + 1]

        flow_img_gray = (corrFlow[:, :, 0, i] ** 2 + corrFlow[:, :, 1, i] ** 2) ** 0.5
        flow_img_gray = flow_img_gray / flow_img_gray.max()

        edge_corr = canny(flow_img_gray, sigma=2, mask=(1 - flow_mask_img).astype(bool))
        edge_completed = infer(args, EdgeGenerator, torch.device('cuda:0'), flow_img_gray, edge_corr, flow_mask_img)
        Edge = np.concatenate((Edge, edge_completed[..., None]), axis=-1)

    return Edge


def video_completion(args):

    # Flow model.
    RAFT_model = initialize_RAFT(args)

    # Loads frames.
    filename_list = glob.glob(os.path.join(args.path, '*.png')) + \
                    glob.glob(os.path.join(args.path, '*.jpg'))

    # Obtains imgH, imgW and nFrame.
    imgH, imgW = np.array(Image.open(filename_list[0])).shape[:2]
    nFrame = len(filename_list)

    # Loads video.
    video = []
    for filename in sorted(filename_list):
        video.append(torch.from_numpy(np.array(Image.open(filename)).astype(np.uint8)[..., :3]).permute(2, 0, 1).float())

    video = torch.stack(video, dim=0)
    video = video.to('cuda')

    # Calcutes the corrupted flow.
    corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB = calculate_flow(args, RAFT_model, video)
    print('\nFinish flow prediction.')

    # Makes sure video is in BGR (opencv) format.
    video = video.permute(2, 3, 1, 0).cpu().numpy()[:, :, ::-1, :] / 255.

    if args.mode == 'video_extrapolation':

        # Creates video and flow where the extrapolated region are missing.
        video, corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB, flow_mask, mask_dilated, start_point, end_point = extrapolation(args, video, corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB)
        imgH, imgW = video.shape[:2]

        # mask indicating the missing region in the video.
        mask = np.tile(flow_mask[..., None], (1, 1, nFrame))
        flow_mask = np.tile(flow_mask[..., None], (1, 1, nFrame))

    else:
        # Loads masks.
        filename_list = glob.glob(os.path.join(args.path_mask, '*.png')) + \
                        glob.glob(os.path.join(args.path_mask, '*.jpg'))

        mask = []
        flow_mask = []
        for filename in sorted(filename_list):
            mask_img = np.array(Image.open(filename).convert('L'))
            mask.append(mask_img)

            # Dilate 15 pixel so that all known pixel is trustworthy
            flow_mask_img = scipy.ndimage.binary_dilation(mask_img, iterations=15)
            # Close the small holes inside the foreground objects
            flow_mask_img = cv2.morphologyEx(flow_mask_img.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((21, 21),np.uint8)).astype(bool)
            flow_mask_img = scipy.ndimage.binary_fill_holes(flow_mask_img).astype(bool)
            flow_mask.append(flow_mask_img)

        # mask indicating the missing region in the video.
        mask = np.stack(mask, -1).astype(bool)
        flow_mask = np.stack(flow_mask, -1).astype(bool)

    if args.edge_guide:
        # Edge completion model.
        EdgeGenerator = EdgeGenerator_()
        EdgeComp_ckpt = torch.load(args.edge_completion_model)
        EdgeGenerator.load_state_dict(EdgeComp_ckpt['generator'])
        EdgeGenerator.to(torch.device('cuda:0'))
        EdgeGenerator.eval()

        # Edge completion.
        FlowF_edge = edge_completion(args, EdgeGenerator, corrFlowF, flow_mask, 'forward')
        FlowB_edge = edge_completion(args, EdgeGenerator, corrFlowB, flow_mask, 'backward')
        print('\nFinish edge completion.')
    else:
        FlowF_edge, FlowB_edge = None, None

    # Completes the flow.
    videoFlowF = complete_flow(args, corrFlowF, flow_mask, 'forward', FlowF_edge)
    videoFlowB = complete_flow(args, corrFlowB, flow_mask, 'backward', FlowB_edge)

    if args.Nonlocal:
        videoNonLocalFlowF = complete_flow(args, corrFlowNLF, flow_mask, 'nonlocal_forward', None)
        videoNonLocalFlowB = complete_flow(args, corrFlowNLB, flow_mask, 'nonlocal_backward', None)
    else:
        videoNonLocalFlowF = None
        videoNonLocalFlowB = None
    print('\nFinish flow completion.')

    iter = 0
    mask_tofill = mask
    video_comp = video

    # Image inpainting model.
    deepfill = DeepFillv1(pretrained_model=args.deepfill_model, image_shape=[imgH, imgW])

    # We iteratively complete the video.
    while(np.sum(mask_tofill) > 0):
        create_dir(os.path.join(args.outroot, 'frame_comp_' + str(iter)))

        # Color propagation.
        video_comp, mask_tofill, _ = get_flowNN(args,
                                      video_comp,
                                      mask_tofill,
                                      videoFlowF,
                                      videoFlowB,
                                      videoNonLocalFlowF,
                                      videoNonLocalFlowB)

        for i in range(nFrame):
            mask_tofill[:, :, i] = scipy.ndimage.binary_dilation(mask_tofill[:, :, i], iterations=2)
            img = video_comp[:, :, :, i] * 255
            # Green indicates the regions that are not filled yet.
            img[mask_tofill[:, :, i]] = [0, 255, 0]
            cv2.imwrite(os.path.join(args.outroot, 'frame_comp_' + str(iter), '%05d.png'%i), img)

        # video_comp_ = (video_comp * 255).astype(np.uint8).transpose(3, 0, 1, 2)[:, :, :, ::-1]
        # imageio.mimwrite(os.path.join(args.outroot, 'frame_comp_' + str(iter), 'intermediate_{0}.mp4'.format(str(iter))), video_comp_, fps=12, quality=8, macro_block_size=1)
        # imageio.mimsave(os.path.join(args.outroot, 'frame_comp_' + str(iter), 'intermediate_{0}.gif'.format(str(iter))), video_comp_, format='gif', fps=12)
        mask_tofill, video_comp = spatial_inpaint(deepfill, mask_tofill, video_comp)
        iter += 1

    create_dir(os.path.join(args.outroot, 'frame_comp_' + 'final'))
    video_comp_ = (video_comp * 255).astype(np.uint8).transpose(3, 0, 1, 2)[:, :, :, ::-1]
    for i in range(nFrame):
        img = video_comp[:, :, :, i] * 255
        cv2.imwrite(os.path.join(args.outroot, 'frame_comp_' + 'final', '%05d.png'%i), img)
        imageio.mimwrite(os.path.join(args.outroot, 'frame_comp_' + 'final', 'final.mp4'), video_comp_, fps=12, quality=8, macro_block_size=1)
        # imageio.mimsave(os.path.join(args.outroot, 'frame_comp_' + 'final', 'final.gif'), video_comp_, format='gif', fps=12)


def video_completion_seamless(args):

    # Flow model.
    RAFT_model = initialize_RAFT(args)

    # Loads frames.
    filename_list = glob.glob(os.path.join(args.path, '*.png')) + \
                    glob.glob(os.path.join(args.path, '*.jpg'))

    # Obtains imgH, imgW and nFrame.
    imgH, imgW = np.array(Image.open(filename_list[0])).shape[:2]
    nFrame = len(filename_list)

    # Loads video.
    video = []
    for filename in sorted(filename_list):
        video.append(torch.from_numpy(np.array(Image.open(filename)).astype(np.uint8)[..., :3]).permute(2, 0, 1).float())

    video = torch.stack(video, dim=0)
    video = video.to('cuda')

    # Calcutes the corrupted flow.
    corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB = calculate_flow(args, RAFT_model, video)
    print('\nFinish flow prediction.')

    # Makes sure video is in BGR (opencv) format.
    video = video.permute(2, 3, 1, 0).cpu().numpy()[:, :, ::-1, :] / 255.

    if args.mode == 'video_extrapolation':

        # Creates video and flow where the extrapolated region are missing.
        video, corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB, flow_mask, mask_dilated, start_point, end_point = extrapolation(args, video, corrFlowF, corrFlowB, corrFlowNLF, corrFlowNLB)
        imgH, imgW = video.shape[:2]

        # mask indicating the missing region in the video.
        mask = np.tile(flow_mask[..., None], (1, 1, nFrame))
        flow_mask = np.tile(flow_mask[..., None], (1, 1, nFrame))
        mask_dilated = np.tile(mask_dilated[..., None], (1, 1, nFrame))

    else:
        # Loads masks.
        filename_list = glob.glob(os.path.join(args.path_mask, '*.png')) + \
                        glob.glob(os.path.join(args.path_mask, '*.jpg'))

        mask = []
        mask_dilated = []
        flow_mask = []
        for filename in sorted(filename_list):
            mask_img = np.array(Image.open(filename).convert('L'))

            # Dilate 15 pixel so that all known pixel is trustworthy
            flow_mask_img = scipy.ndimage.binary_dilation(mask_img, iterations=15)
            # Close the small holes inside the foreground objects
            flow_mask_img = cv2.morphologyEx(flow_mask_img.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((21, 21),np.uint8)).astype(bool)
            flow_mask_img = scipy.ndimage.binary_fill_holes(flow_mask_img).astype(bool)
            flow_mask.append(flow_mask_img)

            mask_img = scipy.ndimage.binary_dilation(mask_img, iterations=5)
            mask_img = scipy.ndimage.binary_fill_holes(mask_img).astype(bool)
            mask.append(mask_img)
            mask_dilated.append(gradient_mask(mask_img))

        # mask indicating the missing region in the video.
        mask = np.stack(mask, -1).astype(bool)
        mask_dilated = np.stack(mask_dilated, -1).astype(bool)
        flow_mask = np.stack(flow_mask, -1).astype(bool)

    if args.edge_guide:
        # Edge completion model.
        EdgeGenerator = EdgeGenerator_()
        EdgeComp_ckpt = torch.load(args.edge_completion_model)
        EdgeGenerator.load_state_dict(EdgeComp_ckpt['generator'])
        EdgeGenerator.to(torch.device('cuda:0'))
        EdgeGenerator.eval()

        # Edge completion.
        FlowF_edge = edge_completion(args, EdgeGenerator, corrFlowF, flow_mask, 'forward')
        FlowB_edge = edge_completion(args, EdgeGenerator, corrFlowB, flow_mask, 'backward')
        print('\nFinish edge completion.')
    else:
        FlowF_edge, FlowB_edge = None, None

    # Completes the flow.
    videoFlowF = complete_flow(args, corrFlowF, flow_mask, 'forward', FlowF_edge)
    videoFlowB = complete_flow(args, corrFlowB, flow_mask, 'backward', FlowB_edge)
    if args.Nonlocal:
        videoNonLocalFlowF = complete_flow(args, corrFlowNLF, flow_mask, 'nonlocal_forward', None)
        videoNonLocalFlowB = complete_flow(args, corrFlowNLB, flow_mask, 'nonlocal_backward', None)
    else:
        videoNonLocalFlowF = None
        videoNonLocalFlowB = None
    print('\nFinish flow completion.')

    # Prepare gradients
    gradient_x = np.empty(((imgH, imgW, 3, 0)), dtype=np.float32)
    gradient_y = np.empty(((imgH, imgW, 3, 0)), dtype=np.float32)

    for indFrame in range(nFrame):
        img = video[:, :, :, indFrame]
        img[mask[:, :, indFrame], :] = 0
        img = cv2.inpaint((img * 255).astype(np.uint8), mask[:, :, indFrame].astype(np.uint8), 3, cv2.INPAINT_TELEA).astype(np.float32)  / 255.

        gradient_x_ = np.concatenate((np.diff(img, axis=1), np.zeros((imgH, 1, 3), dtype=np.float32)), axis=1)
        gradient_y_ = np.concatenate((np.diff(img, axis=0), np.zeros((1, imgW, 3), dtype=np.float32)), axis=0)
        gradient_x = np.concatenate((gradient_x, gradient_x_.reshape(imgH, imgW, 3, 1)), axis=-1)
        gradient_y = np.concatenate((gradient_y, gradient_y_.reshape(imgH, imgW, 3, 1)), axis=-1)

        gradient_x[mask_dilated[:, :, indFrame], :, indFrame] = 0
        gradient_y[mask_dilated[:, :, indFrame], :, indFrame] = 0


    iter = 0
    mask_tofill = mask
    gradient_x_filled = gradient_x # corrupted gradient_x, mask_gradient indicates the missing gradient region
    gradient_y_filled = gradient_y # corrupted gradient_y, mask_gradient indicates the missing gradient region
    mask_gradient = mask_dilated
    video_comp = video

    # Image inpainting model.
    deepfill = DeepFillv1(pretrained_model=args.deepfill_model, image_shape=[imgH, imgW])

    # We iteratively complete the video.
    while(np.sum(mask) > 0):
        create_dir(os.path.join(args.outroot, 'frame_seamless_comp_' + str(iter)))

        # Gradient propagation.
        gradient_x_filled, gradient_y_filled, mask_gradient = \
            get_flowNN_gradient(args,
                                gradient_x_filled,
                                gradient_y_filled,
                                mask,
                                mask_gradient,
                                videoFlowF,
                                videoFlowB,
                                videoNonLocalFlowF,
                                videoNonLocalFlowB)

        # if there exist holes in mask, Poisson blending will fail. So I did this trick. I sacrifice some value. Another solution is to modify Poisson blending.
        for indFrame in range(nFrame):
            mask_gradient[:, :, indFrame] = scipy.ndimage.binary_fill_holes(mask_gradient[:, :, indFrame]).astype(bool)

        # After one gradient propagation iteration
        # gradient --> RGB
        for indFrame in range(nFrame):
            print("Poisson blending frame {0:3d}".format(indFrame))

            if mask[:, :, indFrame].sum() > 0:
                try:
                    frameBlend, UnfilledMask = Poisson_blend_img(video_comp[:, :, :, indFrame], gradient_x_filled[:, 0 : imgW - 1, :, indFrame], gradient_y_filled[0 : imgH - 1, :, :, indFrame], mask[:, :, indFrame], mask_gradient[:, :, indFrame])
                    # UnfilledMask = scipy.ndimage.binary_fill_holes(UnfilledMask).astype(bool)
                except:
                    frameBlend, UnfilledMask = video_comp[:, :, :, indFrame], mask[:, :, indFrame]

                frameBlend = np.clip(frameBlend, 0, 1.0)
                tmp = cv2.inpaint((frameBlend * 255).astype(np.uint8), UnfilledMask.astype(np.uint8), 3, cv2.INPAINT_TELEA).astype(np.float32) / 255.
                frameBlend[UnfilledMask, :] = tmp[UnfilledMask, :]

                video_comp[:, :, :, indFrame] = frameBlend
                mask[:, :, indFrame] = UnfilledMask

                frameBlend_ = copy.deepcopy(frameBlend)
                # Green indicates the regions that are not filled yet.
                frameBlend_[mask[:, :, indFrame], :] = [0, 1., 0]
            else:
                frameBlend_ = video_comp[:, :, :, indFrame]

            cv2.imwrite(os.path.join(args.outroot, 'frame_seamless_comp_' + str(iter), '%05d.png'%indFrame), frameBlend_ * 255.)

        # video_comp_ = (video_comp * 255).astype(np.uint8).transpose(3, 0, 1, 2)[:, :, :, ::-1]
        # imageio.mimwrite(os.path.join(args.outroot, 'frame_seamless_comp_' + str(iter), 'intermediate_{0}.mp4'.format(str(iter))), video_comp_, fps=12, quality=8, macro_block_size=1)
        # imageio.mimsave(os.path.join(args.outroot, 'frame_seamless_comp_' + str(iter), 'intermediate_{0}.gif'.format(str(iter))), video_comp_, format='gif', fps=12)

        mask, video_comp = spatial_inpaint(deepfill, mask, video_comp)
        iter += 1

        # Re-calculate gradient_x/y_filled and mask_gradient
        for indFrame in range(nFrame):
            mask_gradient[:, :, indFrame] = gradient_mask(mask[:, :, indFrame])

            gradient_x_filled[:, :, :, indFrame] = np.concatenate((np.diff(video_comp[:, :, :, indFrame], axis=1), np.zeros((imgH, 1, 3), dtype=np.float32)), axis=1)
            gradient_y_filled[:, :, :, indFrame] = np.concatenate((np.diff(video_comp[:, :, :, indFrame], axis=0), np.zeros((1, imgW, 3), dtype=np.float32)), axis=0)

            gradient_x_filled[mask_gradient[:, :, indFrame], :, indFrame] = 0
            gradient_y_filled[mask_gradient[:, :, indFrame], :, indFrame] = 0

    create_dir(os.path.join(args.outroot, 'frame_seamless_comp_' + 'final'))
    video_comp_ = (video_comp * 255).astype(np.uint8).transpose(3, 0, 1, 2)[:, :, :, ::-1]
    for i in range(nFrame):
        img = video_comp[:, :, :, i] * 255
        cv2.imwrite(os.path.join(args.outroot, 'frame_seamless_comp_' + 'final', '%05d.png'%i), img)
        # imageio.mimwrite(os.path.join(args.outroot, 'frame_seamless_comp_' + 'final', 'final.mp4'), video_comp_, fps=12, quality=8, macro_block_size=1)
        # imageio.mimsave(os.path.join(args.outroot, 'frame_seamless_comp_' + 'final', 'final.gif'), video_comp_, format='gif', fps=12)


def main(args):

    assert args.mode in ('object_removal', 'video_extrapolation'), (
        "Accepted modes: 'object_removal', 'video_extrapolation', but input is %s"
    ) % mode

    if args.seamless:
        video_completion_seamless(args)
    else:
        video_completion(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # video completion
    parser.add_argument('--seamless', action='store_true', help='Whether operate in the gradient domain')
    parser.add_argument('--edge_guide', action='store_true', help='Whether use edge as guidance to complete flow')
    parser.add_argument('--mode', default='object_removal', help="modes: object_removal / video_extrapolation")
    parser.add_argument('--path', default='../data/tennis', help="dataset for evaluation")
    parser.add_argument('--path_mask', default='../data/tennis_mask', help="mask for object removal")
    parser.add_argument('--outroot', default='../result/', help="output directory")
    parser.add_argument('--consistencyThres', dest='consistencyThres', default=np.inf, type=float, help='flow consistency error threshold')
    parser.add_argument('--alpha', dest='alpha', default=0.1, type=float)
    parser.add_argument('--Nonlocal', action='store_true', help='Whether use edge as guidance to complete flow')

    # RAFT
    parser.add_argument('--model', default='../weight/raft-things.pth', help="restore checkpoint")
    parser.add_argument('--small', action='store_true', help='use small model')
    parser.add_argument('--mixed_precision', action='store_true', help='use mixed precision')
    parser.add_argument('--alternate_corr', action='store_true', help='use efficent correlation implementation')

    # Deepfill
    parser.add_argument('--deepfill_model', default='../weight/imagenet_deepfill.pth', help="restore checkpoint")

    # Edge completion
    parser.add_argument('--edge_completion_model', default='../weight/edge_completion.pth', help="restore checkpoint")

    # extrapolation
    parser.add_argument('--H_scale', dest='H_scale', default=2, type=float, help='H extrapolation scale')
    parser.add_argument('--W_scale', dest='W_scale', default=2, type=float, help='W extrapolation scale')

    args = parser.parse_args()

    main(args)
