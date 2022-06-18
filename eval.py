# Copyright 2022 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""Eval"""

import os
import time
import cv2
import numpy as np
from mindspore import DatasetHelper, load_checkpoint, context
from mindspore.nn import Sigmoid

from model_utils.config import base_config
from src.dataset import create_dataset
from src.egnet import build_model


def main(config):
    if config.eval_online:
        import moxing as mox
        mox.file.shift('os', 'mox')
        if config.sal_mode == "t":
            Evalname = "DUTS-TE"
        elif config.sal_mode == "s":
            Evalname = "SOD"
        elif config.sal_mode == "h":
            Evalname = "HKU-IS"
        elif config.sal_mode == "d":
            Evalname = "DUT-OMRON"
        elif config.sal_mode == "p":
            Evalname = "PASCAL-S"
        elif config.sal_mode == "e":
            Evalname = "ECSSD"
        config.test_path = os.path.join("/cache", config.test_path)
        local_data_url = os.path.join(config.test_path, "%s" % (Evalname))
        local_list_eval = os.path.join(config.test_path, "%s/test.lst" % (Evalname))
        mox.file.copy_parallel(config.online_eval_path, local_data_url)
        mox.file.copy_parallel(os.path.join(config.online_eval_path, "test.lst"), local_list_eval)
        ckpt_path = os.path.join("/cache", os.path.dirname(config.model))
        mox.file.copy_parallel(config.online_ckpt_path, ckpt_path)
        mox.file.copy_parallel(os.path.join(config.online_ckpt_path,
                                            os.path.basename(config.model)),
                               os.path.join("/cache", config.model))
        config.model = os.path.join("/cache", config.model)
    context.set_context(mode=context.GRAPH_MODE, device_target=config.device_target)
    test_dataset, dataset = create_dataset(config.test_batch_size, mode="test", num_thread=config.num_thread,
                                           test_mode=config.test_mode, sal_mode=config.sal_mode,
                                           test_path=config.test_path, test_fold=config.test_fold)
    evaluate(test_dataset, config, dataset)


class Metric:
    """
    for metric
    """

    def __init__(self):
        self.epsilon = 1e-4
        self.beta = 0.3
        self.thresholds = 256
        self.mae = 0
        self.max_f = 0
        self.precision = np.zeros(self.thresholds)
        self.recall = np.zeros(self.thresholds)
        self.q = 0
        self.cnt = 0

    def update(self, pred, gt):
        assert pred.shape == gt.shape
        pred = pred.astype(np.float32)
        gt = gt.astype(np.float32)
        norm_pred = pred / 255.0
        norm_gt = gt / 255.0
        self.compute_mae(norm_pred, norm_gt)
        self.compute_precision_and_recall(pred, gt)
        self.compute_s_measure(norm_pred, norm_gt)
        self.cnt += 1

    def print_result(self):
        f_measure = (1 + self.beta) * (self.precision * self.recall) / (self.beta * self.precision + self.recall)
        argmax = np.argmax(f_measure)
        print("Max F-measure:", f_measure[argmax] / self.cnt)
        print("Precision:    ", self.precision[argmax] / self.cnt)
        print("Recall:       ", self.recall[argmax] / self.cnt)
        print("MAE:          ", self.mae / self.cnt)
        print("S-measure:    ", self.q / self.cnt)

    def compute_precision_and_recall(self, pred, gt):
        """
        compute the precision and recall for pred
        """
        for th in range(self.thresholds):
            a = np.zeros_like(pred).astype(np.int32)
            b = np.zeros_like(pred).astype(np.int32)
            a[pred > th] = 1
            a[pred <= th] = 0
            b[gt > th / self.thresholds] = 1
            b[gt <= th / self.thresholds] = 0
            ab = np.sum(np.bitwise_and(a, b))
            a_sum = np.sum(a)
            b_sum = np.sum(b)
            self.precision[th] += (ab + self.epsilon) / (a_sum + self.epsilon)
            self.recall[th] += (ab + self.epsilon) / (b_sum + self.epsilon)

    def compute_mae(self, pred, gt):
        """
        compute mean average error
        """
        self.mae += np.abs(pred - gt).mean()

    def compute_s_measure(self, pred, gt):
        """
        compute s measure score
        """

        alpha = 0.5
        y = gt.mean()
        if y == 0:
            x = pred.mean()
            q = 1.0 - x
        elif y == 1:
            x = pred.mean()
            q = x
        else:
            gt[gt >= 0.5] = 1
            gt[gt < 0.5] = 0
            q = alpha * self._s_object(pred, gt) + (1 - alpha) * self._s_region(pred, gt)
            if q < 0 or np.isnan(q):
                q = 0
        self.q += q

    def _s_object(self, pred, gt):
        """
        score of object
        """
        fg = np.where(gt == 0, np.zeros_like(pred), pred)
        bg = np.where(gt == 1, np.zeros_like(pred), 1 - pred)
        o_fg = self._object(fg, gt)
        o_bg = self._object(bg, 1 - gt)
        u = gt.mean()
        q = u * o_fg + (1 - u) * o_bg
        return q

    @staticmethod
    def _object(pred, gt):
        """
        compute score of object
        """
        temp = pred[gt == 1]
        if temp.size == 0:
            return 0
        x = temp.mean()
        sigma_x = temp.std()
        score = 2.0 * x / (x * x + 1.0 + sigma_x + 1e-20)
        return score

    def _s_region(self, pred, gt):
        """
        compute score of region
        """
        x, y = self._centroid(gt)
        gt1, gt2, gt3, gt4, w1, w2, w3, w4 = self._divide_gt(gt, x, y)
        p1, p2, p3, p4 = self._divide_prediction(pred, x, y)
        q1 = self._ssim(p1, gt1)
        q2 = self._ssim(p2, gt2)
        q3 = self._ssim(p3, gt3)
        q4 = self._ssim(p4, gt4)
        q = w1 * q1 + w2 * q2 + w3 * q3 + w4 * q4
        return q

    @staticmethod
    def _divide_gt(gt, x, y):
        """
        divide ground truth image
        """
        if not isinstance(x, np.int64):
            x = x[0][0]
        if not isinstance(y, np.int64):
            y = y[0][0]
        h, w = gt.shape[-2:]
        area = h * w
        gt = gt.reshape(h, w)
        lt = gt[:y, :x]
        rt = gt[:y, x:w]
        lb = gt[y:h, :x]
        rb = gt[y:h, x:w]
        x = x.astype(np.float32)
        y = y.astype(np.float32)
        w1 = x * y / area
        w2 = (w - x) * y / area
        w3 = x * (h - y) / area
        w4 = 1 - w1 - w2 - w3
        return lt, rt, lb, rb, w1, w2, w3, w4

    @staticmethod
    def _divide_prediction(pred, x, y):
        """
        divide predict image
        """
        if not isinstance(x, np.int64):
            x = x[0][0]
        if not isinstance(y, np.int64):
            y = y[0][0]
        h, w = pred.shape[-2:]
        pred = pred.reshape(h, w)
        lt = pred[:y, :x]
        rt = pred[:y, x:w]
        lb = pred[y:h, :x]
        rb = pred[y:h, x:w]
        return lt, rt, lb, rb

    @staticmethod
    def _ssim(pred, gt):
        """
        structural similarity
        """
        gt = gt.astype(np.float32)
        h, w = pred.shape[-2:]
        n = h * w
        x = pred.mean()
        y = gt.mean()
        sigma_x2 = ((pred - x) * (pred - x)).sum() / (n - 1 + 1e-20)
        sigma_y2 = ((gt - y) * (gt - y)).sum() / (n - 1 + 1e-20)
        sigma_xy = ((pred - x) * (gt - y)).sum() / (n - 1 + 1e-20)

        alpha = 4 * x * y * sigma_xy
        beta = (x * x + y * y) * (sigma_x2 + sigma_y2)

        if alpha != 0:
            q = alpha / (beta + 1e-20)
        elif alpha == 0 and beta == 0:
            q = 1.0
        else:
            q = 0
        return q

    @staticmethod
    def _centroid(gt):
        """
        compute center of ground truth image
        """
        rows, cols = gt.shape[-2:]
        gt = gt.reshape(rows, cols)
        if gt.sum() == 0:
            x = np.eye(1) * round(cols / 2)
            y = np.eye(1) * round(rows / 2)
        else:
            total = gt.sum()

            i = np.arange(0, cols).astype(np.float32)
            j = np.arange(0, rows).astype(np.float32)
            x = np.round((gt.sum(axis=0) * i).sum() / total)
            y = np.round((gt.sum(axis=1) * j).sum() / total)
        return x.astype(np.int64), y.astype(np.int64)


def evaluate(test_ds, config, dataset):
    """build network"""
    model = build_model(config.base_model)
    # Load pretrained model
    load_checkpoint(config.model, net=model)
    print(f"Loading pre-trained model from {config.model}...")
    sigmoid = Sigmoid()
    # test phase
    test_save_name = config.test_save_name + config.base_model
    test_fold = config.test_fold
    if not os.path.exists(os.path.join(test_fold, test_save_name)):
        os.makedirs(os.path.join(test_fold, test_save_name), exist_ok=True)
    dataset_helper = DatasetHelper(test_ds, epoch_num=1, dataset_sink_mode=False)
    time_t = 0.0

    metric = Metric()
    has_label = False
    for i, data_batch in enumerate(dataset_helper):
        sal_image, sal_label, name_index = data_batch[0], data_batch[1], data_batch[2]
        name = dataset.image_list[name_index[0].asnumpy().astype(np.int32)]
        save_file = os.path.join(test_fold, test_save_name, name[:-4] + "_sal.png")
        directory, _ = os.path.split(save_file)
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        time_start = time.time()
        _, _, up_sal_final = model(sal_image)
        time_end = time.time()
        time_t += time_end - time_start
        pred = sigmoid(up_sal_final[-1]).asnumpy().squeeze() * 255

        if sal_label is not None:
            has_label = True
            sal_label = sal_label.asnumpy().squeeze() * 255
            pred = np.round(pred).astype(np.uint8)
            metric.update(pred, sal_label)
        cv2.imwrite(save_file, pred)
        print(f"process image index {i} done")

    print(f"--- {time_t} seconds ---")
    if has_label:
        metric.print_result()


if __name__ == "__main__":
    main(base_config)
