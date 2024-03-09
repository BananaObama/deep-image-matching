import threading
import shutil
import json
import os
import warnings
import subprocess
from pathlib import Path
from PIL import Image

import h5py
import numpy as np
from tqdm import tqdm

from .. import logger

__OPENMVG_INTRINSIC_NAME_MAP = {
    'pinhole': 'pinhole',
    'to_do': 'pinhole_radial_k3',
    'to_do': 'pinhole_brown_t2'
}


def loadJSON(sfm_data):
  with open(sfm_data) as file:
    sfm_data = json.load(file)
  view_ids = {view['value']['ptr_wrapper']['data']['filename']:view['key'] for view in sfm_data['views']}
  image_paths = [os.path.join(sfm_data['root_path'], view['value']['ptr_wrapper']['data']['filename']) for view in sfm_data['views']]
  return view_ids, image_paths

def saveFeaturesOpenMVG(matches_folder, basename, keypoints):
  with open(os.path.join(matches_folder, f'{basename}.feat'), 'w') as feat:
    for x, y in keypoints:
      feat.write(f'{x} {y} 1.0 0.0\n')

def saveDescriptorsOpenMVG(matches_folder, basename, descriptors):
  with open(os.path.join(matches_folder, f'{basename}.desc'), 'wb') as desc:
    desc.write(len(descriptors).to_bytes(8, byteorder='little'))
    desc.write(((descriptors.numpy() + 1) * 0.5 * 255).round(0).astype(np.ubyte).tobytes())

def saveMatchesOpenMVG(matches, out_folder):
  with open(out_folder / 'matches.putative.bin', 'wb') as bin:
    bin.write((1).to_bytes(1, byteorder='little'))
    bin.write(len(matches).to_bytes(8, byteorder='little'))
    for index1, index2, idxs in matches:
      bin.write(index1.tobytes())
      bin.write(index2.tobytes())
      bin.write(len(idxs).to_bytes(8, byteorder='little'))
      bin.write(idxs.tobytes())
  shutil.copyfile(out_folder / 'matches.putative.bin', out_folder / 'matches.f.bin')

def add_keypoints(h5_path, image_path, matches_dir):
    keypoint_f = h5py.File(str(h5_path), "r")
    for filename in tqdm(list(keypoint_f.keys())):
        keypoints = keypoint_f[filename]["keypoints"].__array__()
        name = Path(filename).stem

        path = os.path.join(image_path, filename)
        if not os.path.isfile(path):
            raise IOError(f"Invalid image path {path}")
        if len(keypoints.shape) >= 2:
            threading.Thread(target=lambda: saveFeaturesOpenMVG(matches_dir, name, keypoints)).start()
            #threading.Thread(target=lambda: saveDescriptorsOpenMVG(matches_dir, filename, features.descriptors)).start()
    return

def add_matches(h5_path, sfm_data, matches_dir):
    view_ids, image_paths = loadJSON(sfm_data)
    putative_matches = []

    match_file = h5py.File(str(h5_path), "r")
    added = set()
    n_keys = len(match_file.keys())
    n_total = (n_keys * (n_keys - 1)) // 2

    with tqdm(total=n_total) as pbar:
        for key_1 in match_file.keys():
            group = match_file[key_1]
            for key_2 in group.keys():
                id_1 = view_ids[key_1]
                id_2 = view_ids[key_2]
                if (key_1, key_2) in added:
                    warnings.warn(f"Pair ({key_1}, {key_2}) already added!")
                    continue
                matches = group[key_2][()]
                putative_matches.append([np.int32(id_1), np.int32(id_2), matches.astype(np.int32)])
                added.add((key_1, key_2))
                pbar.update(1)
    match_file.close()
    saveMatchesOpenMVG(putative_matches, matches_dir)

def update_scene_with_camera_options(images_dir : Path, camera_options : dict):
    """
    Inspired by PySfMUtils https://gitlab.com/educelab/sfm-utils/-/blob/develop/sfm_utils/openmvg.py?ref_type=heads
    images_dir : path to directory containing all the images
    camera_options : dictionary with all the options from config/cameras.yaml
    """
    # Emulate the Cereal pointer counter
    __ptr_cnt = 2147483649

    def open_mvg_view(id : int, img_name: str, images_dir : Path, images_cameras : dict) -> dict:
        """
        OpenMVG View struct
        images_cameras : dictionary with image names as key and camera id as value
        """
        image_path = images_dir / img_name
        image = Image.open(image_path)
        width, height = image.size

        nonlocal __ptr_cnt
        d = {
            "key": id,
            "value": {
                "polymorphic_id": 1073741824,
                "ptr_wrapper": {
                    "id": __ptr_cnt,
                    "data": {
                        "local_path": '',
                        "filename": img_name,
                        "width": width,
                        "height": height,
                        "id_view": id,
                        "id_intrinsic": images_cameras[img_name],
                        "id_pose": id,
                    }
                }
            }
        }
        __ptr_cnt += 1
        return d

    def open_mvg_intrinsic(intrinsic: dict) -> dict:
        """
        OpenMVG Intrinsic struct
        """
        print(intrinsic)
        nonlocal __ptr_cnt
        d = {
            'key': intrinsic['cam_id'],
            'value': {
                'polymorphic_id': 2147483649,
                "polymorphic_name": __OPENMVG_INTRINSIC_NAME_MAP[intrinsic['camera_model']],
                "ptr_wrapper": {
                    "id": __ptr_cnt,
                    "data": {
                        "width": 800,
                        "height": 533,
                        "focal_length": 623,
                        "principal_point": [
                            400.0,
                            266.5
                        ]
                    }
                }
            }
        }
        __ptr_cnt += 1
#
#        if intrinsic.dist_params is not None:
#            dist_name = __OPENMVG_DIST_NAME_MAP[intrinsic.type]
#            d['value']['ptr_wrapper']['data'][dist_name] = intrinsic.dist_params
#
        return d

    images = os.listdir(images_dir)
    images_cameras = {}
    intrinsics = {}
    cam = 0
    for key in list(camera_options.keys()):
       imgs = camera_options[key]['images']
       imgs = imgs.split(',')
       if imgs[0] == '':
          pass
       else:
          for img in imgs:
            images_cameras[img] = cam
            intrinsics[cam] = {'cam_id' : cam, 'camera_model' : camera_options[key]['camera_model']}
          cam += 1
    
    grouped_imgs = list(images_cameras.keys())
    for img in images:
      if img not in grouped_imgs:
        images_cameras[img] = cam
        intrinsics[cam] = {'cam_id' : cam, 'camera_model' : camera_options['general']['camera_model']}
        cam =+ 1
    
    # Construct OpenMVG struct
    data = {
        'sfm_data_version': '0.3',
        'root_path': str(images_dir),
        'views': [open_mvg_view(i, img, images_dir, images_cameras) for i,img in enumerate(images)],
        'intrinsics': [open_mvg_intrinsic(intrinsics[c]) for c in list(intrinsics.keys())],
        'extrinsics': [],
        'structure': [],
        'control_points': []
    }
    #print('check', data); quit()

    return data


def export_to_openmvg(
    img_dir,
    feature_path: Path,
    match_path: Path,
    openmvg_out_path: Path,
    openmvg_sfm_bin: Path,
    openmvg_database: Path,
    camera_options: dict,
):
    if openmvg_out_path.exists():
        logger.warning(f"OpenMVG output folder {openmvg_out_path} already exists - deleting it")
        os.rmdir(openmvg_out_path)
    os.makedirs(openmvg_out_path)

    #camera_file_params = openmvg_sfm_bin / "sensor_width_database" / "sensor_width_camera_database.txt"
    #camera_file_params = "/home/threedom/Desktop/prova/sensor_width_database/sensor_width_camera_database.txt"
    camera_file_params = openmvg_database
    matches_dir = openmvg_out_path / "matches"

    pIntrisics = subprocess.Popen( [os.path.join(openmvg_sfm_bin, "openMVG_main_SfMInit_ImageListing"),  "-i", img_dir, "-o", matches_dir, "-d", camera_file_params] )
    pIntrisics.wait()

    print(camera_options)
    prova = update_scene_with_camera_options(img_dir, camera_options)


    import json
    #with open(matches_dir / "sfm_data.json", 'r') as file:
    #  data = json.load(file)
    #  print(data)
    #  quit()

    #prova = {'sfm_data_version': '0.3', 'root_path': 'assets\\pytest\\images', 'views': [{'key': 0, 'value': {'polymorphic_id': 1073741824, 'ptr_wrapper': {'id': 2147483649, 'data': {'local_path': '', 'filename': 'DSC_6466.jpg', 'width': 800, 'height': 533, 'id_view': 0, 'id_intrinsic': 0, 'id_pose': 0}}}}, {'key': 1, 'value': {'polymorphic_id': 1073741824, 'ptr_wrapper': {'id': 2147483650, 'data': {'local_path': '', 'filename': 'DSC_6467.jpg', 'width': 800, 'height': 533, 'id_view': 1, 'id_intrinsic': 0, 'id_pose': 1}}}}, {'key': 2, 'value': {'polymorphic_id': 1073741824, 'ptr_wrapper': {'id': 2147483651, 'data': {'local_path': '', 'filename': 'DSC_6468.jpg', 'width': 800, 'height': 533, 'id_view': 2, 'id_intrinsic': 0, 'id_pose': 2}}}}], 'intrinsics': [{'key': 0, 'value': {'polymorphic_id': 2147483649, 'polymorphic_name': 'pinhole_radial_k3', 'ptr_wrapper': {'id': 2147483652, 'data': {'width': 800, 'height': 533, 'focal_length': 623.9554317548747, 'principal_point': [400.0, 266.5], 'disto_k3': [0.0, 0.0, 0.0]}}}}], 'extrinsics': [], 'structure': [], 'control_points': []}
    #prova = {'sfm_data_version': '0.3', 'root_path': 'assets\\pytest\\images', 'views': [{'key': 0, 'value': {'polymorphic_id': 1073741824, 'ptr_wrapper': {'id': 2147483649, 'data': {'local_path': '', 'filename': 'DSC_6466.jpg', 'width': 800, 'height': 533, 'id_view': 0, 'id_intrinsic': 1, 'id_pose': 0}}}}, {'key': 1, 'value': {'polymorphic_id': 1073741824, 'ptr_wrapper': {'id': 2147483650, 'data': {'local_path': '', 'filename': 'DSC_6467.jpg', 'width': 800, 'height': 533, 'id_view': 1, 'id_intrinsic': 0, 'id_pose': 1}}}}, {'key': 2, 'value': {'polymorphic_id': 1073741824, 'ptr_wrapper': {'id': 2147483651, 'data': {'local_path': '', 'filename': 'DSC_6468.jpg', 'width': 800, 'height': 533, 'id_view': 2, 'id_intrinsic': 0, 'id_pose': 2}}}}], 'intrinsics': [{'key': 0, 'value': {'polymorphic_id': 2147483649, 'polymorphic_name': 'pinhole_radial_k3', 'ptr_wrapper': {'id': 2147483652, 'data': {'width': 800, 'height': 533, 'focal_length': 623.9554317548747, 'principal_point': [400.0, 266.5], 'disto_k3': [0.0, 0.0, 0.0]}}}}, {'key': 1, 'value': {'polymorphic_id': 2147483650, 'polymorphic_name': 'pinhole_radial_k3', 'ptr_wrapper': {'id': 2147483653, 'data': {'width': 800, 'height': 533, 'focal_length': 623.9554317548747, 'principal_point': [400.0, 266.5], 'disto_k3': [0.0, 0.0, 0.0]}}}}], 'extrinsics': [], 'structure': [], 'control_points': []}


    with open(matches_dir / "sfm_data.json", 'w') as json_file:
      json.dump(prova, json_file, indent=2) 

    add_keypoints(feature_path, img_dir, matches_dir)
    add_matches(match_path, openmvg_out_path / "matches" / "sfm_data.json", matches_dir)

    return