import argparse
from concurrent.futures import ThreadPoolExecutor
import math
import os
import sys
import time
from pathlib import Path
from PIL import Image
from threading import Thread

import cv2
cv2.setNumThreads(0)

# Suppress tensorflow warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import components.models.facenet as facenet
import components.models.mtcnn as mtcnn
from util import config
from util.consts import (
    FILE_BBOXES,
    FILE_EMBEDS,
    FILE_METADATA,
    DIR_CROPS
)
from util.utils import json_is_valid, save_json, format_hmmss

MODELS_DIR = 'components/data'


NAMED_COMPONENTS = [
    'face_detection',
    'face_embedding',
    'face_crops',
]


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('in_path', help=('path to mp4 or to a text file '
                                         'containing video filepaths'))
    parser.add_argument('out_path', help='path to output directory')
    parser.add_argument('-i', '--init-run', action='store_true',
                        help='running on videos for the first time')
    parser.add_argument('-f', '--force-rerun', action='store_true',
                        help='force rerun for all videos')
    parser.add_argument('--interval', type=int, default=config.INTERVAL,
                        help='interval length in seconds')
    parser.add_argument('-d', '--disable', nargs='+', choices=NAMED_COMPONENTS,
                        help='list of named components to disable')
    return parser.parse_args()


def main(in_path, out_path, init_run=False, force=False,
         interval=config.INTERVAL, disable=None):
    if in_path.endswith('.mp4'):
        video_paths = [Path(in_path)]
    else:
        video_paths = [Path(l.strip()) for l in open(in_path, 'r') if l.strip()]

    out_paths = [Path(out_path)/p.stem for p in video_paths]
    for p in out_paths:
        p.mkdir(parents=True, exist_ok=True)

    process_videos(video_paths, out_paths, init_run, force, interval, disable)


def process_videos(video_paths, out_paths, init_run=False, force=False,
                   interval=config.INTERVAL, disable=None):
    assert len(video_paths) == len(out_paths), ('Mismatch between video and '
                                                'output paths')

    if disable is None:
        disable = []

    # Don't reingest videos with existing outputs
    if not init_run and not force:
        for i in range(len(video_paths) - 1, -1, -1):
            if (('face_detection' in disable
                or json_is_valid(os.path.join(out_paths[i], FILE_BBOXES)))
                and ('face_embeddings' in disable
                    or json_is_valid(os.path.join(out_paths[i], FILE_EMBEDS)))
                and json_is_valid(os.path.join(out_paths[i], FILE_METADATA))
                and ('face_crops' in disable
                    or os.path.isdir(os.path.join(out_paths[i], DIR_CROPS)))
            ):
                video_paths.pop(i)
                out_paths.pop(i)

    video_names = [vid.stem for vid in video_paths]
    if not video_names:
        print('All videos have existing outputs.')
        sys.stdout.flush()
        return

    face_embedder, face_detector = load_models(MODELS_DIR)

    print('Collecting metadata for {} videos'.format(len(video_names)))
    sys.stdout.flush()
    all_metadata = [
        get_video_metadata(video_names[i], video_paths[i])
        for i in range(len(video_names))
    ]

    n_threads = os.cpu_count() if os.cpu_count() else 1

    total_sec = int(sum(math.floor(m['frames'] / m['fps'] / interval) for m in all_metadata))

    done_sec = 0
    start_time = time.time()
    for vid_id in range(len(video_names)):
        path = video_paths[vid_id]
        meta = all_metadata[vid_id]

        print('Processing video: {} ({:0.1f} % done, {} elapsed)'.format(
            meta['name'], done_sec / total_sec * 100, 
            format_hmmss(time.time() - start_time)))
        sys.stdout.flush()
        thread_bboxes = [[] for _ in range(n_threads)]
        thread_crops = [[] for _ in range(n_threads)]
        thread_embeddings = [[] for _ in range(n_threads)]

        threads = [Thread(
            target=thread_task,
            args=(str(path), meta, interval, n_threads, i, thread_bboxes, thread_crops,
                  thread_embeddings, face_embedder, face_detector)
        ) for i in range(n_threads)]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        all_bboxes = []
        all_crops = []
        all_embeddings = []
        for i in range(n_threads):
            all_bboxes += thread_bboxes[i]
            all_crops += thread_crops[i]
            all_embeddings += thread_embeddings[i]

        target_sec = math.floor(meta['frames'] / meta['fps'] / interval)
        if any(len(x) != target_sec for x in [all_bboxes, all_crops, all_embeddings]):
            # Error decoding video
            print('\nThere was an error decoding video \'{}\'. Skipping.'.format(meta['name']))
            sys.stdout.flush()
            continue

        print('Saving metadata for {}'.format(meta['name']))
        sys.stdout.flush()
        out_path = out_paths[vid_id]
        metadata_outpath = out_path/FILE_METADATA
        save_json(meta, str(metadata_outpath))

        print('Saving bboxes for {}'.format(meta['name']))
        sys.stdout.flush()
        bbox_outpath = out_path/FILE_BBOXES
        handle_face_bboxes_results(all_bboxes, meta['fps'] * interval, str(bbox_outpath))

        print('Saving embeddings for {}'.format(meta['name']))
        sys.stdout.flush()
        embed_outpath = out_path/FILE_EMBEDS
        handle_face_embeddings_results(all_embeddings, str(embed_outpath))

        print('Saving crops for {}'.format(meta['name']))
        sys.stdout.flush()
        crops_outpath = out_path/DIR_CROPS
        handle_face_crops_results(all_crops, str(crops_outpath))

        done_sec += target_sec

    print('Processed {} videos in {}'.format(
        len(video_names), format_hmmss(time.time() - start_time)))
    sys.stdout.flush()

    face_embedder.close()
    face_detector.close()


def load_models(models_dir):
    face_embedder = facenet.FaceNetEmbed(os.path.join(models_dir, 'facenet'))
    face_detector = mtcnn.MTCNN(os.path.join(models_dir, 'align'))
    return face_embedder, face_detector


def get_video_metadata(video_name: str, video_path: Path):
    video = cv2.VideoCapture(str(video_path))
    return {
        'name': video_name,
        'fps': video.get(cv2.CAP_PROP_FPS),
        'frames': int(video.get(cv2.CAP_PROP_FRAME_COUNT)),
        'width': int(video.get(cv2.CAP_PROP_FRAME_WIDTH)),
        'height': int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    }


BATCH_SIZE = 16
def thread_task(in_path, metadata, interval, n_threads, thread_id,
                thread_bboxes, thread_crops, thread_embeddings, face_embedder,
                face_detector):

    video = cv2.VideoCapture(in_path)

    if not video.isOpened():
        print('Error opening video file.', in_path)
        sys.stdout.flush()
        return

    n_sec = math.floor(metadata['frames'] / metadata['fps'] / interval)
    chunk_size_sec = math.floor(n_sec / n_threads)
    start_sec = chunk_size_sec * thread_id
    if thread_id == n_threads - 1:
        chunk_size_sec += n_sec % n_threads
    end_sec = start_sec + chunk_size_sec

    for sec in range(start_sec, end_sec, BATCH_SIZE):
        frames = []

        for i in range(sec, min(sec + BATCH_SIZE, end_sec)):
            frame_num = math.ceil(i * metadata['fps'] * interval)
            video.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            success, frame = video.read()
            if not success:
                video.release()
                return

            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(frame)

        detected_faces = face_detector.face_detect(frames)
        dilated_bboxes = [dilate_bboxes(x) if x else [] for x in detected_faces]

        # Cropped images to compute embeddings on
        crops = [[crop_bbox(f, bb) for bb in x] if x else []
                 for f, x in zip(frames, dilated_bboxes)]
        embeddings = [face_embedder.embed(c) if c else [] for c in crops]

        # Cropped images being saved
        crops = [[crop_bbox(f, bb, expand=0.1, square=True) for bb in x] if x else []
                 for f, x in zip(frames, dilated_bboxes)]

        thread_bboxes[thread_id].extend(detected_faces)
        thread_embeddings[thread_id].extend(embeddings)
        thread_crops[thread_id].extend(crops)

    video.release()


DILATE_AMOUNT = 1.05
def dilate_bboxes(detected_faces):
    return [{
        'x1': bbox['x1'] * (2 - DILATE_AMOUNT),
        'x2': bbox['x2'] * DILATE_AMOUNT,
        'y1': bbox['y1'] * (2 - DILATE_AMOUNT),
        'y2': bbox['y2'] * DILATE_AMOUNT
    } for bbox in detected_faces]


def crop_bbox(img, bbox, expand=0.0, square=False):
    y1 = max(bbox['y1'] - expand, 0)
    y2 = min(bbox['y2'] + expand, 1)
    x1 = max(bbox['x1'] - expand, 0)
    x2 = min(bbox['x2'] + expand, 1)
    h, w = img.shape[:2]
    cropped = img[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w), :]

    if not square:
        return cropped

    # Crop largest square
    if cropped.shape[0] > cropped.shape[1]:
        target_height = cropped.shape[1]
        diff = target_height // 2
        center = cropped.shape[0] // 2
        square = cropped[center - diff:center + (target_height - diff), :, :]
    else:
        target_width = cropped.shape[0]
        diff = target_width // 2
        center = cropped.shape[1] // 2
        square = cropped[:, center - diff:center + (target_width - diff), :]

    return square


def handle_face_bboxes_results(detected_faces, stride, outpath: str):
    result = []  # [(<face_id>, {'frame_num': <n>, 'bbox': <bbox_dict>}), ...]
    for i, faces in enumerate(detected_faces):
        faces_in_frame = [
            (face_id, {'frame_num': math.ceil(i * stride), 'bbox': face})
            for face_id, face in enumerate(faces, len(result))
        ]

        result += faces_in_frame

    save_json(result, outpath)


def handle_face_embeddings_results(face_embeddings, outpath):
    result = []  # [(<face_id>, <embedding>), ...]
    for embeddings in face_embeddings:
        faces_in_frame = [
            (face_id, [float(x) for x in embed])
            for face_id, embed in enumerate(embeddings, len(result))
        ]

        result += faces_in_frame

    save_json(result, outpath)


def handle_face_crops_results(face_crops, out_dirpath):
    # Results are too large to transmit
    results = get_face_crops_results(face_crops)
    save_face_crops(results, out_dirpath)


def get_face_crops_results(face_crops):
    result = []  # [(<face_id>, <crop>)]
    for crops in face_crops:
        faces_in_frame = [
            (face_id, img) for face_id, img in enumerate(crops, len(result))
        ]

        result += faces_in_frame

    return result


def save_face_crops(face_crops, out_dirpath: str):
    if not os.path.isdir(out_dirpath):
        os.makedirs(out_dirpath)

    def save_img(img, fp):
        Image.fromarray(img).save(fp, optimize=True)

    with ThreadPoolExecutor() as executor:
        for face_id, img in face_crops:
            img_filepath = os.path.join(out_dirpath, str(face_id) + '.png')
            executor.submit(save_img, img, img_filepath)
