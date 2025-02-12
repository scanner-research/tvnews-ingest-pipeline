#!/usr/bin/env python3

"""
File: identify_faces_with_aws.py
--------------------------------
Script for identifying faces from face crops through AWS.

Example
-------

    in_path:  output_dir
    out_path: output_dir

    where 'output_dir' contains video output subdirectories (which in turn
    contain their own 'crops' directories)

    outputs

        output_dir/
        ├── video1
        │   └── identities.json
        └── video2
            └── identities.json

where there is one JSON file per video containing a list of (face_id, identity)
tuples.

"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import math
from multiprocessing import Pool
import os
import sys
from pathlib import Path
import time

import boto3

from util import config
from util.consts import FILE_IDENTITIES, DIR_CROPS
from components.montage_face_images import create_montage_bytes
from util.utils import get_base_name, load_json, save_json, format_hmmss


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('in_path', help=('path to directory for a single '
            'video or to a directory with subdirectories for each video'))
    parser.add_argument('out_path', help='path to output directory')
    parser.add_argument('-f', '--force', action='store_true',
                        help='force overwrite existing output')
    return parser.parse_args()


def main(in_path, out_path, force=False):
    if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
        print('AWS credentials do not exist. Skipping face identification.')
        sys.stdout.flush()
        return

    video_names = list(os.listdir(in_path))
    out_paths = [Path(out_path)/name for name in video_names]
    in_path = Path(in_path)
    for p in out_paths:
        p.mkdir(parents=True, exist_ok=True)

    # Prune videos that should not be run
    msg = []
    for i in range(len(video_names) - 1, -1, -1):
        crops_path = in_path/video_names[i]/DIR_CROPS
        if not crops_path.is_dir():
            msg.append("Skipping face identification for video '{}': no '{}' "
                       "directory found.".format(video_names[i], DIR_CROPS))
            video_names.pop(i)
            out_paths.pop(i)
            continue

        identities_outpath = out_paths[i]/FILE_IDENTITIES
        if not force and identities_outpath.exists():
            msg.append("Skipping face identification for video '{}': '{}' "
                       "already exists.".format(video_names[i], FILE_IDENTITIES))
            video_names.pop(i)
            out_paths.pop(i)

    if not video_names:
        print('All videos have existing face identities.')
        sys.stdout.flush()
        return

    if msg:
        print(*msg, sep='\n')
        sys.stdout.flush()

    num_workers = min(len(video_names), 4)
    num_threads_per_worker = 60 // num_workers  # prevent throttling

    start_time = time.time()
    print('Identifying faces in {} videos'.format(len(video_names)))
    with Pool(num_workers) as workers:
        for video_name, output_dir in zip(video_names, out_paths):
            crops_path = in_path/video_name/DIR_CROPS
            identities_outpath = output_dir/FILE_IDENTITIES
            workers.apply_async(
                process_video,
                args=(str(crops_path), str(identities_outpath),
                      num_threads_per_worker))

        workers.close()
        workers.join()
    print('Done identifying faces. {} elapased'.format(
        format_hmmss(time.time() - start_time)))


def process_video(crops_path, identities_outpath, max_threads=60):
    assert os.path.isdir(crops_path)

    img_files = [img for img in sorted(os.listdir(crops_path),
                 key=lambda x: int(get_base_name(x)))]

    video_labels = []
    n_rows = config.MONTAGE_HEIGHT
    n_cols = config.MONTAGE_WIDTH
    with ThreadPoolExecutor(max_threads) as executor:
        futures = []
        for i in range(0, len(img_files), n_cols * n_rows):
            img_span = img_files[i:i + n_cols * n_rows]
            futures.append(executor.submit(
                submit_images_for_labeling, crops_path, img_span
            ))

    for future in futures:
        if future.done() and not future.cancelled():
            video_labels.extend(future.result())

    save_json(video_labels, identities_outpath)


def submit_images_for_labeling(crops_path, img_files):
    try:
        client = load_client()
        img_filepaths = [os.path.join(crops_path, f) for f in img_files]
        montage_bytes, meta = create_montage_bytes(img_filepaths)
        if len(montage_bytes) >= 5 * 1024 * 1024:
            img_filepaths_1 = img_filepaths[:len(img_filepaths)//2]
            img_filepaths_2 = img_filepaths[len(img_filepaths)//2:]
            montage_bytes_1, meta_1 = create_montage_bytes(img_filepaths_1)
            montage_bytes_2, meta_2 = create_montage_bytes(img_filepaths_2)
            img_ids_1 = [int(get_base_name(x)) for x in img_files[:len(img_files)//2]]
            img_ids_2 = [int(get_base_name(x)) for x in img_files[len(img_files)//2:]]
            res_1 = search_aws(montage_bytes_1, client)
            res_2 = search_aws(montage_bytes_2, client)

            labels_1 = process_labeling_results(meta_1['cols'], meta_1['block_dim'],
                                                img_ids_1, res_1)
            labels_2 = process_labeling_results(meta_2['cols'], meta_2['block_dim'],
                                                img_ids_2, res_2)
            return labels_1 + labels_2

        img_ids = [int(get_base_name(x)) for x in img_files]

        res = search_aws(montage_bytes, client)
        return process_labeling_results(meta['cols'], meta['block_dim'],
                                        img_ids, res)

    except AssertionError as e:
        print(e)
        sys.stdout.flush()
        raise

    except Exception as e:
        print(e)
        sys.stdout.flush()
        raise



def search_aws(img_data, client):
    # Supported image formats: JPEG, PNG, GIF, BMP.
    # Image dimensions must be at least 50 x 50.
    # Image file size must be less than 5MB.
    assert len(img_data) <= 5 * 1024 * 1024, 'File too large: {}'.format(len(img_data))
    for i in range(10):
        try:
            resp = client.recognize_celebrities(Image={'Bytes': img_data})
            return resp
        except Exception as e:
            delay = 2 ** i
            print('Error (retry in {}s): {}'.format(delay, e))
            sys.stdout.flush()
            time.sleep(delay)
    raise Exception('Too many timeouts: {}'.format(e))


def process_labeling_results(
    n_cols, block_size, img_ids, aws_response, img_draw=None
):
    half_block_size = int(block_size / 2)
    sixth_block_size = int(block_size / 6)
    width = n_cols * block_size
    height = math.ceil(len(img_ids) / n_cols) * block_size

    labels = {}
    for face in aws_response['CelebrityFaces']:
        x0 = face['Face']['BoundingBox']['Left']
        y0 = face['Face']['BoundingBox']['Top']
        x1 = x0 + face['Face']['BoundingBox']['Width']
        y1 = y0 + face['Face']['BoundingBox']['Height']

        if img_draw:
            img_draw.rectangle(
                (x0 * width, y0 * height, x1 * width, y1 * height),
                outline='red')
            text = '{}, {}'.format(
                face['Name'].encode('ascii', 'ignore'), face['MatchConfidence'])
            img_draw.text((x0 * width, y0 * height), text, fill='red')

        # Reverse map the index
        center_x = (x0 + x1) / 2 * width
        center_y = (y0 + y1) / 2 * height
        residual_x = abs(center_x % block_size - half_block_size)
        residual_y = abs(center_y % block_size - half_block_size)

        # Center must be in middle third of the image
        if (residual_x < sixth_block_size and residual_y < sixth_block_size):
            grid_x = math.floor(center_x / block_size)
            grid_y = math.floor(center_y / block_size)
            idx = grid_y * n_cols + grid_x
            if idx >= 0 and idx < len(img_ids):
                face_id = img_ids[idx]
                l1_dist = residual_x + residual_y
                face_label = (face['Name'], face['MatchConfidence'], l1_dist,
                              grid_x, grid_y)
                if face_id in labels:
                    if labels[face_id][2] > l1_dist:
                        labels[face_id] = face_label
                else:
                    labels[face_id] = face_label

    for face in aws_response['UnrecognizedFaces']:
        x0 = face['BoundingBox']['Left']
        y0 = face['BoundingBox']['Top']
        x1 = x0 + face['BoundingBox']['Width']
        y1 = y0 + face['BoundingBox']['Height']

        if img_draw:
            img_draw.rectangle(
                (x0 * width, y0 * height, x1 * width, y1 * height),
                outline='blue')

        # Reverse map the index
        center_x = (x0 + x1) / 2 * width
        center_y = (y0 + y1) / 2 * height
        residual_x = abs(center_x % block_size - half_block_size)
        residual_y = abs(center_y % block_size - half_block_size)

        # Center must be in middle of the image
        if (residual_x < half_block_size and residual_y < half_block_size):
            grid_x = math.floor(center_x / block_size)
            grid_y = math.floor(center_y / block_size)
            idx = grid_y * n_cols + grid_x
            if idx >= 0 and idx < len(img_ids):
                face_id = img_ids[idx]
                l1_dist = residual_x + residual_y
                if face_id in labels:
                    if labels[face_id][2] > l1_dist:
                        del labels[face_id]

    if img_draw:
        for label_meta in labels.values():
            name, _, _, grid_x, grid_y = label_meta
            img_draw.text((grid_x * block_size, grid_y * block_size),
                          name.encode('ascii', 'ignore'), fill='red')


    return [(k, v[0], v[1]) for k, v in labels.items()]


def load_client():
    session = boto3.session.Session()
    return session.client('rekognition', aws_access_key_id=config.AWS_ACCESS_KEY_ID,
                          aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
                          region_name=config.AWS_REGION)


if __name__ == '__main__':
    main(**vars(get_args()))
