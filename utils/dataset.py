import os
import cv2
import random
import numpy as np
import tensorflow as tf
import threading
from queue import Queue
from .utils import image_preporcess 

class Dataset(object):
    """implement Dataset here"""
    def __init__(self, dataset_type, sample_rate=1.0, pworker=3):
        self.annot_path  = "./data/{}.ano".format(("train" if dataset_type == "train" else "test"))
        self.batch_size  = 16
        self.data_aug    = True
        self.sample_rate = 1

        self.train_input_sizes = [224, 288, 320, 352, 448]
        self.strides = np.array([16, 32])
        self.num_classes = 8
        self.anchors = np.reshape(np.array([1.59375,2.46875,2.0,2.3125,2.21875,3.1875,1.359375,1.390625,1.546875,1.828125,2.265625,3.0]), (2, 3, 2)) # np.reshape(np.array([1.875, 3.8125, 3.875, 2.8125, 3.6875, 7.4375, 3.625, 2.8125, 4.875, 6.1875, 11.65625, 10.1875]), (2, 3, 2))
        self.anchor_per_scale = 3
        self.max_bbox_per_scale = 150

        self.annotations = self.load_annotations(dataset_type)
        self.num_samples = len(self.annotations)
        self.num_batchs = int(np.ceil(1.0 * self.num_samples / self.batch_size * self.sample_rate))
        self.read_index = 0
        self.queue = Queue(32) 
        self.pworker = pworker
        self.lock = threading.Lock()
        self.threads = [threading.Thread(target=self.produce_task).start() for x in range(self.pworker)]


    def load_annotations(self, dataset_type):
        with open(self.annot_path, 'r') as f:
            txt = f.readlines()
            annotations = [line.strip() for line in txt if len(line.strip().split()) != 0]
        np.random.shuffle(annotations)
        return annotations

    def __iter__(self):
        return self

    def next(self):
        return self.__next__()

    def generator(self):
        yield self.__next__()

    def gen_iter(self):
        while(True):
            for x in self:
                yield x

    @property
    def batch_count(self):
        return self.read_index // self.batch_size

    def produce(self):
        train_input_size = random.choice(self.train_input_sizes)
        train_output_sizes = train_input_size // self.strides

        batch_image = np.zeros((self.batch_size, train_input_size, train_input_size, 3))

        batch_label_mbbox = np.zeros((self.batch_size, train_output_sizes[0], train_output_sizes[0],
                                      self.anchor_per_scale, 5 + self.num_classes))
        batch_label_lbbox = np.zeros((self.batch_size, train_output_sizes[1], train_output_sizes[1],
                                      self.anchor_per_scale, 5 + self.num_classes))

        batch_mbboxes = np.zeros((self.batch_size, self.max_bbox_per_scale, 4))
        batch_lbboxes = np.zeros((self.batch_size, self.max_bbox_per_scale, 4))

        num = 0
        while num < self.batch_size:
            with self.lock:
                self.read_index += 1
                index = self.read_index
            if index >= self.num_samples: index %= self.num_samples
            annotation = self.annotations[index]
            image, bboxes = self.parse_annotation(annotation, train_input_size)

            label_mbbox, label_lbbox  = self.preprocess_true_boxes(bboxes, train_output_sizes)

            batch_image[num, :, :, :] = image
            batch_label_mbbox[num, :, :, :, :] = label_mbbox
            batch_label_lbbox[num, :, :, :, :] = label_lbbox
            num += 1

        return batch_image, [batch_label_mbbox, batch_label_lbbox]

    def produce_task(self):
        while(True):
            result = self.produce()
            self.queue.put(result, block=True)

    def __next__(self):
        if self.batch_count < self.num_batchs:
            return self.queue.get(block=True)
        else:
            with self.lock:
                self.read_index = 0
                np.random.shuffle(self.annotations)
            raise StopIteration
           

    def random_horizontal_flip(self, image, bboxes):

        if random.random() < 0.5:
            _, w, _ = image.shape
            image = image[:, ::-1, :]
            if len(bboxes) > 0:
                bboxes[:, [0,2]] = w - bboxes[:, [2,0]]

        return image, bboxes

    def random_crop(self, image, bboxes):
        has_box = len(bboxes) > 0

        if random.random() < 0.5:
            h, w, _ = image.shape
            if has_box:
                max_bbox = np.concatenate([np.min(bboxes[:, 0:2], axis=0), np.max(bboxes[:, 2:4], axis=0)], axis=-1)
            else:
                max_bbox = np.array([random.uniform(0, 0.15)] * 2 + [random.uniform(0.85, 1)] * 2) * np.array([w, h, w, h])

            max_l_trans = max_bbox[0]
            max_u_trans = max_bbox[1]
            max_r_trans = w - max_bbox[2]
            max_d_trans = h - max_bbox[3]

            crop_xmin = max(0, int(max_bbox[0] - random.uniform(0, max_l_trans)))
            crop_ymin = max(0, int(max_bbox[1] - random.uniform(0, max_u_trans)))
            crop_xmax = max(w, int(max_bbox[2] + random.uniform(0, max_r_trans)))
            crop_ymax = max(h, int(max_bbox[3] + random.uniform(0, max_d_trans)))

            image = image[crop_ymin : crop_ymax, crop_xmin : crop_xmax]

            if has_box:
                bboxes[:, [0, 2]] = bboxes[:, [0, 2]] - crop_xmin
                bboxes[:, [1, 3]] = bboxes[:, [1, 3]] - crop_ymin

        return image, bboxes

    def rotate(self, img, bboxes, range_degree=(-10, 10)):
        """ 
            given a face with bbox and landmark, rotate with alpha
            and return rotated face with bbox, landmark (absolute position)
        """
        alpha = random.uniform(*range_degree)
        height, width = img.shape[:2]
        center = (width // 2, height // 2)
        rot_mat = cv2.getRotationMatrix2D(center, alpha, 1)

        #whole image rotate
        #pay attention: 3rd param(col*row)
        img = cv2.warpAffine(img, rot_mat, (width, height))
        for bbox in bboxes:
            left_up, right_down = bbox[:2], bbox[2:4]
            right_up, left_down = bbox[0:4:3], bbox[2:0:-1]
            points = np.stack([left_up, right_up, left_down, right_down]).astype(np.float32)
            ones = np.ones(shape=(len(points), 1))

            points_ones = np.hstack([points, ones])
            rpoints = rot_mat.dot(points_ones.T).T.astype(np.int)
            rpoints[:, 0] = np.clip(rpoints[:, 0], 0, width)
            rpoints[:, 1] = np.clip(rpoints[:, 1], 0, height)
            lu, rd = (min(rpoints[:, 0]), min(rpoints[:, 1])), (max(rpoints[:, 0]), max(rpoints[:, 1]))
            bbox[:4] = np.array(list(lu + rd))
            #cv2.rectangle(img, lu, rd, (0, 255, 0), 2)

        #cv2.imwrite("gg.jpg", img)
        return img, bboxes


    def random_translate(self, image, bboxes):
        has_box = len(bboxes) > 0
        if random.random() < 0.5:
            h, w, _ = image.shape
            if has_box:
                max_bbox = np.concatenate([np.min(bboxes[:, 0:2], axis=0), np.max(bboxes[:, 2:4], axis=0)], axis=-1)
            else:
                max_bbox = np.array([random.uniform(0, 0.15)] * 2 + [random.uniform(0.85, 1)] * 2) * np.array([w, h, w, h])

            max_l_trans = max_bbox[0]
            max_u_trans = max_bbox[1]
            max_r_trans = w - max_bbox[2]
            max_d_trans = h - max_bbox[3]

            tx = random.uniform(-(max_l_trans - 1), (max_r_trans - 1))
            ty = random.uniform(-(max_u_trans - 1), (max_d_trans - 1))

            M = np.array([[1, 0, tx], [0, 1, ty]])
            image = cv2.warpAffine(image, M, (w, h))

            if has_box:
                bboxes[:, [0, 2]] = bboxes[:, [0, 2]] + tx
                bboxes[:, [1, 3]] = bboxes[:, [1, 3]] + ty

        return image, bboxes

    def parse_annotation(self, annotation, train_input_size):
        # non-box, all 0
        line = annotation.split()
        image_path = line[0]
        if not os.path.exists(image_path):
            raise KeyError("%s does not exist ... " %image_path)
        image = np.array(cv2.imread(image_path))
        bboxes = np.array([list(map(lambda x: int(float(x)), box.split(','))) for box in line[1:]])

        if self.data_aug:
            image, bboxes = self.random_horizontal_flip(np.copy(image), np.copy(bboxes))
            image, bboxes = self.random_crop(np.copy(image), np.copy(bboxes))
            image, bboxes = self.random_translate(np.copy(image), np.copy(bboxes))
            image, bboxes = self.rotate(np.copy(image), np.copy(bboxes))
            
        image, bboxes = image_preporcess(np.copy(image),
                [train_input_size, train_input_size],
                np.copy(bboxes))
        return image, bboxes

    def bbox_iou(self, boxes1, boxes2):

        boxes1 = np.array(boxes1)
        boxes2 = np.array(boxes2)

        boxes1_area = boxes1[..., 2] * boxes1[..., 3]
        boxes2_area = boxes2[..., 2] * boxes2[..., 3]

        boxes1 = np.concatenate([boxes1[..., :2] - boxes1[..., 2:] * 0.5,
                                boxes1[..., :2] + boxes1[..., 2:] * 0.5], axis=-1)
        boxes2 = np.concatenate([boxes2[..., :2] - boxes2[..., 2:] * 0.5,
                                boxes2[..., :2] + boxes2[..., 2:] * 0.5], axis=-1)

        left_up = np.maximum(boxes1[..., :2], boxes2[..., :2])
        right_down = np.minimum(boxes1[..., 2:], boxes2[..., 2:])

        inter_section = np.maximum(right_down - left_up, 0.0)
        inter_area = inter_section[..., 0] * inter_section[..., 1]
        union_area = boxes1_area + boxes2_area - inter_area

        return inter_area / union_area

    def preprocess_true_boxes(self, bboxes, train_output_sizes):

        label = [np.zeros((train_output_sizes[i], train_output_sizes[i], self.anchor_per_scale,
                           5 + self.num_classes)) for i in range(2)]
        bbox_count = np.zeros((2,))

        for bbox in bboxes:
            bbox_coor = bbox[:4]
            bbox_class_ind = bbox[4]

            onehot = np.zeros(self.num_classes, dtype=np.float)
            onehot[bbox_class_ind] = 1.0
            uniform_distribution = np.full(self.num_classes, 1.0 / self.num_classes)
            deta = 0.01
            smooth_onehot = onehot * (1 - deta) + deta * uniform_distribution

            bbox_xywh = np.concatenate([(bbox_coor[2:] + bbox_coor[:2]) * 0.5, bbox_coor[2:] - bbox_coor[:2]], axis=-1)  # center, w/h
            bbox_xywh_scaled = 1.0 * bbox_xywh[np.newaxis, :] / self.strides[:, np.newaxis]

            iou = []
            exist_positive = False
            for i in range(2):
                anchors_xywh = np.zeros((self.anchor_per_scale, 4))
                anchors_xywh[:, 0:2] = np.floor(bbox_xywh_scaled[i, 0:2]).astype(np.int32) + 0.5
                anchors_xywh[:, 2:4] = self.anchors[i]

                iou_scale = self.bbox_iou(bbox_xywh_scaled[i][np.newaxis, :], anchors_xywh)
                iou.append(iou_scale)
                iou_mask = iou_scale > 0.3

                if np.any(iou_mask):
                    xind, yind = np.floor(bbox_xywh_scaled[i, 0:2]).astype(np.int32)

                    try:
                        label[i][yind, xind, iou_mask, :] = 0
                        label[i][yind, xind, iou_mask, 0:4] = bbox_xywh
                        label[i][yind, xind, iou_mask, 4:5] = 1.0
                        label[i][yind, xind, iou_mask, 5:] = smooth_onehot
                    except Exception as ee:
                        print(bbox)
                        print(bbox_xywh_scaled[i])
                        print(yind, xind)
                        raise ee

                    bbox_ind = int(bbox_count[i] % self.max_bbox_per_scale)
                    bbox_count[i] += 1

                    exist_positive = True

        label_mbbox, label_lbbox = label
        return label_mbbox, label_lbbox

    def __len__(self):
        return self.num_batchs
