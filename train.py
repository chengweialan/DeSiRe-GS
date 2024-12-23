#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#
import json
import os
import glob
from collections import defaultdict
import torch
import torch.nn.functional as F
import random
from random import randint
from utils.loss_utils import psnr, ssim, tv_loss
from gaussian_renderer import get_renderer
from scene import Scene, GaussianModel, EnvLight
from utils.general_utils import seed_everything, visualize_depth, init_logging
from tqdm import tqdm
from argparse import ArgumentParser
from torchvision.utils import make_grid, save_image
import logging
from omegaconf import OmegaConf, DictConfig
from pprint import pprint, pformat
from texttable import Texttable
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

EPS = 1e-5
non_zero_mean = (
    lambda x: sum(x) / len(x) if len(x) > 0 else -1
)
def training(args):
    
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
        logging.info("Logging progress to Tensorboard")
    else:
        tb_writer = None
        logging.info("Tensorboard not available: not logging progress")
    vis_path = os.path.join(args.model_path, 'visualization')
    os.makedirs(vis_path, exist_ok=True)
    
    render_func, render_wrapper = get_renderer(args.render_type)
    
    gaussians = GaussianModel(args)
    
    scene = Scene(args, gaussians)

    gaussians.training_setup(args)
    
    if args.env_map_res > 0:
        env_map = EnvLight(resolution=args.env_map_res).cuda()
        env_map.training_setup(args)
    else:
        env_map = None

    first_iter = 0
    if args.resume:
        checkpoints = glob.glob(os.path.join(args.model_path, "chkpnt*.pth"))
        assert len(checkpoints) > 0, "No checkpoints found."
        checkpoint = sorted(checkpoints, key=lambda x: int(x.split("chkpnt")[-1].split(".")[0]))[-1]
        logging.info(f"Loading checkpoint {checkpoint}")
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, args)
        
        order = first_iter // args.sh_increase_interval
        for _ in range(order):
            scene.upScale()
        
        if env_map is not None:
            env_checkpoint = os.path.join(os.path.dirname(checkpoint), 
                                        os.path.basename(checkpoint).replace("chkpnt", "env_light_chkpnt"))
            (light_params, _) = torch.load(env_checkpoint)
            env_map.restore(light_params)

    bg_color = [1, 1, 1] if args.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    # iter_start = torch.cuda.Event(enable_timing = True)
    # iter_end = torch.cuda.Event(enable_timing = True)

    viewpoint_stack = None

    ema_dict_for_log = defaultdict(int)
    progress_bar = tqdm(range(1, args.iterations + 1), desc="Training", bar_format='{l_bar}{bar:50}{r_bar}')
    # update the progress bar to the first iteration
    progress_bar.update(first_iter + 1)
    
    for iteration in range(first_iter + 1, args.iterations + 1):       
        # iter_start.record()
        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % args.sh_increase_interval == 0:
            gaussians.oneupSHdegree()

        if not viewpoint_stack:
            viewpoint_stack = list(range(len(scene.getTrainCameras())))
        camera_id = viewpoint_stack.pop(randint(0, len(viewpoint_stack) - 1))
        viewpoint_cam = scene.getTrainCameras()[camera_id]
        nearest_cam = None if len(viewpoint_cam.nearest_id) == 0 else scene.getTrainCameras()[random.sample(viewpoint_cam.nearest_id,1)[0]]

        loss, log_dict, render_pkg = render_wrapper(args, viewpoint_cam, gaussians, background, scene.time_interval, env_map, iteration, camera_id, nearest_cam=nearest_cam)
        
        loss.backward()
        log_dict['loss'] = loss.item()
        
        # iter_end.record()

        with torch.no_grad():
            for key in ["psnr"]: # 'loss', "loss_l1", 
                ema_dict_for_log[key] = 0.4 * log_dict[key] + 0.6 * ema_dict_for_log[key]

            # log_dict['iter_time'] = iter_start.elapsed_time(iter_end)
            log_dict['total_points'] = gaussians.get_xyz.shape[0]
                           
            if iteration % 10 == 0:
                postfix = {k[5:] if k.startswith("loss_") else k:f"{ema_dict_for_log[k]:.{5}f}" for k, v in ema_dict_for_log.items()}
                postfix["scale"] = scene.resolution_scales[scene.scale_index]
                postfix["pts"] = gaussians.get_xyz.shape[0]
                progress_bar.set_postfix(postfix)
                progress_bar.update(10)


            # Log and save
            complete_eval(tb_writer, iteration, args.test_iterations, scene, render_func, (args, background), 
                          log_dict, env_map=env_map)

            # Densification
            if iteration > args.densify_until_iter * args.time_split_frac:
                gaussians.no_time_split = False

            if iteration < args.densify_until_iter and (args.densify_until_num_points < 0 or gaussians.get_xyz.shape[0] < args.densify_until_num_points):
                viewspace_point_tensor = render_pkg["viewspace_points"]
                visibility_filter = render_pkg["visibility_filter"]
                radii = render_pkg["radii"]
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)
                if iteration > args.densify_from_iter and iteration % args.densification_interval == 0:
                    size_threshold = args.size_threshold if (iteration > args.opacity_reset_interval and args.prune_big_point > 0) else None

                    if size_threshold is not None:
                        size_threshold = size_threshold // scene.resolution_scales[0]

                    gaussians.densify_and_prune(args.densify_grad_threshold, args.thresh_opa_prune, scene.cameras_extent, size_threshold, args.densify_grad_t_threshold)

                if iteration % args.opacity_reset_interval == 0 or (args.white_background and iteration == args.densify_from_iter):
                    gaussians.reset_opacity()

            gaussians.optimizer.step()
            gaussians.optimizer.zero_grad(set_to_none = True)
            if env_map is not None and iteration < args.env_optimize_until:
                env_map.optimizer.step()
                env_map.optimizer.zero_grad(set_to_none = True)
            torch.cuda.empty_cache()

            if iteration % args.vis_step == 0 or iteration == 1:
                save_visualizations(render_pkg, viewpoint_cam, vis_path, iteration)
            
            if iteration % args.scale_increase_interval == 0:
                scene.upScale()

            if iteration in args.checkpoint_iterations:
                logging.info("[ITER {}] Saving Checkpoint".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")
                torch.save((env_map.capture(), iteration), scene.model_path + "/env_light_chkpnt" + str(iteration) + ".pth")
                if gaussians.uncertainty_model is not None:
                    gaussians.uncertainty_model.save(scene.model_path + "/uncertainty_model" + str(iteration) + ".pth")


def complete_eval(tb_writer, iteration, test_iterations, scene : Scene, renderFunc, renderArgs, log_dict, env_map=None):
    from lpipsPyTorch import lpips

    if tb_writer and iteration % 10 == 0:
        for key, value in log_dict.items():
            tb_writer.add_scalar(f'train/{key}', value, iteration)

    if iteration in test_iterations:
        scale = scene.resolution_scales[scene.scale_index]
        if iteration < args.iterations:
            validation_configs = ({'name': 'test', 'cameras': scene.getTestCameras(scale=scale)}, {'name': 'train', 'cameras': scene.getTrainCameras()})
        else:
            if "kitti" in args.model_path:
                # follow NSG: https://github.com/princeton-computational-imaging/neural-scene-graphs/blob/8d3d9ce9064ded8231a1374c3866f004a4a281f8/data_loader/load_kitti.py#L766
                num = len(scene.getTrainCameras())//2
                eval_train_frame = num//5
                traincamera = sorted(scene.getTrainCameras(), key =lambda x: x.colmap_id)
                validation_configs = ({'name': 'test', 'cameras': scene.getTestCameras(scale=scale)},
                                    {'name': 'train', 'cameras': traincamera[:num][-eval_train_frame:]+traincamera[num:][-eval_train_frame:]})
            else:
                validation_configs = ({'name': 'test', 'cameras': scene.getTestCameras(scale=scale)},
                                {'name': 'train', 'cameras': scene.getTrainCameras()})



        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = []
                psnr_test = []
                ssim_test = []
                lpips_test = []
                masked_psnr_test = []
                masked_ssim_test = []
                depth_error = []
                outdir = os.path.join(args.model_path, "eval", config['name'] + f"_{iteration}" + "_render")
                os.makedirs(outdir,exist_ok=True)
                for idx, viewpoint in enumerate(tqdm(config['cameras'], desc="Evaluating", bar_format='{l_bar}{bar:50}{r_bar}')):
                    v = scene.gaussians.get_inst_velocity
                    t_scale = scene.gaussians.get_scaling_t.clamp_max(2)
                    other = [t_scale, v]
                    render_pkg = renderFunc(viewpoint, scene.gaussians, *renderArgs, env_map=env_map, other=other, is_training=False)
                    image = torch.clamp(render_pkg["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    
                    depth = render_pkg['depth']
                    alpha = render_pkg['alpha']
                    normal = render_pkg['normal']
                    sky_depth = 900
                    depth = depth / alpha.clamp_min(EPS)
                    if env_map is not None:
                        if args.depth_blend_mode == 0:  # harmonic mean
                            depth = 1 / (alpha / depth.clamp_min(EPS) + (1 - alpha) / sky_depth).clamp_min(EPS)
                        elif args.depth_blend_mode == 1:
                            depth = alpha * depth + (1 - alpha) * sky_depth
                    pts_depth = viewpoint.pts_depth.cuda()
                    mask = (pts_depth > 0)
                    depth_error.append(F.l1_loss(depth[mask], pts_depth[mask]).double().item())
                    feature = render_pkg['feature'] / alpha.clamp_min(1e-5)
                    t_map = feature[0:1]
                    v_map = feature[1:]
                    v_norm_map = v_map.norm(dim=0, keepdim=True)

                    et_color = visualize_depth(t_map, near=0.01, far=1)
                    v_color = visualize_depth(v_norm_map, near=0.01, far=1)     
                              
                    sky_mask = viewpoint.sky_mask.to("cuda")
                    
                    if scene.gaussians.uncertainty_model is not None:
                        _, _, dynamic_mask, dino_cosine = scene.gaussians.uncertainty_model.get_loss(gt_image, image.detach(), sky_mask)
                    else:
                        dynamic_mask = viewpoint.dynamic_mask.to("cuda") if viewpoint.dynamic_mask is not None else torch.zeros_like(alpha, dtype=torch.bool)     
                        dino_cosine = torch.ones_like(depth)    
                    dynamic_mask = (dynamic_mask > 1 - EPS).to(dtype=dynamic_mask.dtype)  
                    depth = visualize_depth(depth)
                    alpha = alpha.repeat(3, 1, 1)

                    grid = [image, alpha, depth, et_color, \
                        gt_image, normal, dynamic_mask.float().repeat(3, 1, 1), v_color]
                    grid = make_grid(grid, nrow=4)

                    save_image(grid, os.path.join(outdir, f"{viewpoint.colmap_id:03d}.png"))

                    l1_test.append(F.l1_loss(image, gt_image).double().item())
                    psnr_test.append(psnr(image, gt_image).double().item())
                    ssim_test.append(ssim(image, gt_image).double().item())
                    lpips_test.append(lpips(image, gt_image, net_type='vgg').double().item())  # very slow

                    if dynamic_mask.sum() > 0:
                        dynamic_mask = dynamic_mask.repeat(3, 1, 1) > 0 # (C, H, W)
                        masked_psnr_test.append(psnr(image[dynamic_mask], gt_image[dynamic_mask]).double().item())
                        unaveraged_ssim = ssim(image, gt_image, size_average=False) # (C, H, W)
                        masked_ssim_test.append(unaveraged_ssim[dynamic_mask].mean().double().item())
        

                psnr_test = non_zero_mean(psnr_test)
                l1_test = non_zero_mean(l1_test)
                ssim_test = non_zero_mean(ssim_test)
                lpips_test = non_zero_mean(lpips_test)
                masked_psnr_test = non_zero_mean(masked_psnr_test)
                masked_ssim_test = non_zero_mean(masked_ssim_test)
                depth_error = non_zero_mean(depth_error)
                    
                t = Texttable()
                t.add_rows([["PSNR", "SSIM", "LPIPS", "L1", "PSNR (dynamic)", "SSIM (dynamic)", "Depth Error"], 
                            [f"{psnr_test:.4f}", f"{ssim_test:.4f}", f"{lpips_test:.4f}", f"{l1_test:.4f}", f"{masked_psnr_test:.4f}", f"{masked_ssim_test:.4f}", f"{depth_error:.4f}"]])
                logging.info(t.draw())   
                             
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - ssim', ssim_test, iteration)
                with open(os.path.join(args.model_path, "eval", "metrics_{}.json".format(iteration)), "a+") as f:
                    json.dump({"split": config['name'], "iteration": iteration,
                        "psnr": psnr_test, "ssim": ssim_test, "lpips": lpips_test, "masked_psnr": masked_psnr_test, "masked_ssim": masked_ssim_test, "depth_error": depth_error,
                        }, f)        
                torch.cuda.empty_cache()


def save_visualizations(render_pkg, viewpoint_cam, vis_path, iteration):
    alpha = torch.clamp(render_pkg["alpha"], 0.0, 1.0)
    depth = render_pkg["depth"]
    image = render_pkg["render"]
    rendered_normal = torch.clamp(render_pkg.get("normal", torch.zeros_like(image)), 0.0, 1.0)
    sky_mask = viewpoint_cam.sky_mask.cuda() if viewpoint_cam.sky_mask is not None else torch.zeros_like(alpha, dtype=torch.bool)
    gt_image = viewpoint_cam.original_image.cuda()
    gt_normal = viewpoint_cam.normal_map.cuda() if viewpoint_cam.normal_map is not None else torch.zeros_like(rendered_normal)
    pseudo_normal = torch.clamp(render_pkg.get("depth_normal", gt_normal), 0.0, 1.0)
    # other_img = []
    feature = render_pkg['feature'] / alpha.clamp_min(1e-5)
    t_map = feature[0:1] # (1, H, W)
    v_map = feature[1:] # (1, H, W)
    v_norm_map = v_map.norm(dim=0, keepdim=True)
    et_color = visualize_depth(t_map, near=0.01, far=1)
    v_color = visualize_depth(v_norm_map, near=0.01, far=1)
    # other_img.append(et_color)
    # other_img.append(v_color)
    dynamic_mask = render_pkg['dynamic_mask']
    
    if viewpoint_cam.pts_depth is not None:
        pts_depth = viewpoint_cam.pts_depth
        # pts_depth[pts_depth == 0] = 900
        pts_depth_vis = visualize_depth(pts_depth)
        # other_img.append(pts_depth_vis)
    
    not_sky_mask = torch.logical_not(sky_mask[:1]).float()  
    rendered_distance = render_pkg.get("rendered_distance", torch.zeros_like(alpha))
    # d_mask = render_pkg.get("d_mask", torch.zeros_like(alpha))
    grid = make_grid([
        image, 
        alpha.repeat(3, 1, 1),
        visualize_depth(depth),
        rendered_normal,
        et_color,
        visualize_depth(rendered_distance),
        # dino_cosine.repeat(3, 1, 1), 
        gt_image,
        not_sky_mask.repeat(3, 1, 1),
        pts_depth_vis,
        # gt_normal * not_sky_mask,
        pseudo_normal,
        dynamic_mask.repeat(3, 1, 1),
        v_color,
        # d_mask.repeat(3, 1, 1),
    ], nrow=6)

    save_image(grid, os.path.join(vis_path, f"{iteration:05d}_{viewpoint_cam.colmap_id:03d}.png"))

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--base_config", type=str, default = "configs/base.yaml")
    args, _ = parser.parse_known_args()
    
    base_conf = OmegaConf.load(args.base_config)
    second_conf = OmegaConf.load(args.config)
    cli_conf = OmegaConf.from_cli()
    args = OmegaConf.merge(base_conf, second_conf, cli_conf)
  
    args.save_iterations.append(args.iterations)
    args.checkpoint_iterations.append(args.iterations)
    args.test_iterations.append(args.iterations)

    if args.exhaust_test:
        args.test_iterations += [i for i in range(0,args.iterations, args.test_interval)]
    os.makedirs(args.model_path, exist_ok=True)    
    init_logging(os.path.join(args.model_path, "train.log"))
    logging.info("PID:{}".format(os.getpid()))
    logging.info("Optimizing " + args.model_path)  
    logging.info('Configurations:\n {}'.format(pformat(OmegaConf.to_container(args, resolve=True, throw_on_missing=True))))
    # write config to yaml file
    OmegaConf.save(args, os.path.join(args.model_path, "config.yaml"))
    seed_everything(args.seed)

    training(args)

    # All done
    logging.info("Training complete.")
