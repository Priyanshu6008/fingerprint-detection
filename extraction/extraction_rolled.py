from skimage.color import rgb2gray
from skimage import io
import numpy as np
import glob
import sys
import timeit
import argparse
import scipy
import cv2
import get_maps
import preprocessing
import descriptor
import os
import template
import minutiae_AEC_modified as minutiae_AEC
import json
import descriptor_PQ
import descriptor_DR

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'


class FeatureExtraction_Rolled:
    def __init__(self, patch_types=None, des_model_dirs=None, minu_model_dir=None):
        self.des_models = None
        self.patch_types = patch_types
        self.minu_model = None
        self.minu_model_dir = minu_model_dir
        self.des_model_dirs = des_model_dirs

        print("Loading models, this may take some time...")
        if self.minu_model_dir is not None:
            print("Loading minutiae model: " + minu_model_dir)
            self.minu_model = (minutiae_AEC.ImportGraph(minu_model_dir))

        self.dict, self.spacing, self.dict_all, self.dict_ori, self.dict_spacing = get_maps.construct_dictionary(
            ori_num=24)
        patchSize = 160
        oriNum = 64
        if des_model_dirs is not None and len(des_model_dirs) > 0:
            self.patchIndexV = descriptor.get_patch_index(patchSize, patchSize, oriNum, isMinu=1)

        if self.des_model_dirs is not None:
            self.des_models = []
            for i, model_dir in enumerate(des_model_dirs):
                print("Loading descriptor model (" + str(i+1) + " of " + str(len(des_model_dirs)) + "): " + model_dir)
                self.des_models.append(descriptor.ImportGraph(model_dir, input_name="inputs:0",
                                                              output_name='embedding:0'))
            self.patch_size = 96

    def remove_spurious_minutiae(self, mnt, mask):
        minu_num = len(mnt)
        if minu_num <= 0:
            return mnt
        flag = np.ones((minu_num,), np.uint8)
        h, w = mask.shape[:2]
        R = 5
        for i in range(minu_num):
            x = mnt[i, 0]
            y = mnt[i, 1]
            x = np.int(x)
            y = np.int(y)
            if x < R or y < R or x > w-R-1 or y > h-R-1:
                flag[i] = 0
            elif mask[y-R, x-R] == 0 or mask[y-R, x+R] == 0 or mask[y+R, x-R] == 0 or mask[y+R, x+R] == 0:
                flag[i] = 0
        mnt = mnt[flag > 0, :]
        return mnt

    def feature_extraction_single_rolled(self, img_file, output_dir=None, ppi=500):
        block_size = 16

        if not os.path.exists(img_file):
            return None
        img = io.imread(img_file, s_grey=True)
        if ppi != 500:
            img = cv2.resize(img, (0, 0), fx=500.0/ppi, fy=500.0/ppi)

        img = preprocessing.adjust_image_size(img, block_size)
        if len(img.shape) > 2:
            img = rgb2gray(img)
        h, w = img.shape
        start = timeit.default_timer()
        mask = get_maps.get_quality_map_intensity(img)
        stop = timeit.default_timer()
        print('time for cropping : %f' % (stop - start))
        start = timeit.default_timer()
        contrast_img = preprocessing.local_constrast_enhancement(img)
        texture_img = preprocessing.FastCartoonTexture(img, sigma=2.5, show=False)
        mnt = self.minu_model.run_whole_image(texture_img, minu_thr=0.15)
        stop = timeit.default_timer()
        print('time for minutiae : %f' % (stop - start))

        start = timeit.default_timer()
        des = descriptor.minutiae_descriptor_extraction(img, mnt, self.patch_types, self.des_models, self.patchIndexV,
                                                        batch_size=256, patch_size=self.patch_size)
        stop = timeit.default_timer()
        print('time for descriptor : %f' % (stop - start))

        dir_map, _ = get_maps.get_maps_STFT(img, patch_size=64, block_size=block_size, preprocess=True)

        blkH = h // block_size
        blkW = w // block_size

        minu_template = template.MinuTemplate(h=h, w=w, blkH=blkH, blkW=blkW, minutiae=mnt, des=des, oimg=dir_map,
                                              mask=mask)

        rolled_template = template.Template()
        rolled_template.add_minu_template(minu_template)

        start = timeit.default_timer()
        # texture templates
        stride = 16

        x = np.arange(24, w - 24, stride)
        y = np.arange(24, h - 24, stride)

        virtual_minutiae = []
        distFromBg = scipy.ndimage.morphology.distance_transform_edt(mask)
        for y_i in y:
            for x_i in x:
                if (distFromBg[y_i][x_i] <= 24):
                    continue
                ofY = int(y_i / 16)
                ofX = int(x_i / 16)

                ori = -dir_map[ofY][ofX]
                virtual_minutiae.append([x_i, y_i, ori])
        virtual_minutiae = np.asarray(virtual_minutiae)

        if len(virtual_minutiae) > 1000:
            virtual_minutiae = virtual_minutiae[:1000]
        print len(virtual_minutiae)
        if len(virtual_minutiae) > 3:
            virtual_des = descriptor.minutiae_descriptor_extraction(contrast_img, virtual_minutiae, self.patch_types,
                                                                    self.des_models,
                                                                    self.patchIndexV,
                                                                    batch_size=128)
            texture_template = template.TextureTemplate(h=h, w=w, minutiae=virtual_minutiae, des=virtual_des,
                                                        mask=mask)
            rolled_template.add_texture_template(texture_template)
        stop = timeit.default_timer()
        print('time for texture : %f' % (stop - start))
        return rolled_template

    def feature_extraction(self, image_dir, img_type='bmp', template_dir=None, enhancement=False):

        img_files = glob.glob(image_dir + '*.' + img_type)
        assert(len(img_files) > 0)
        img_files.sort()

        for i, img_file in enumerate(img_files):
            print img_file

            start = timeit.default_timer()
            img_name = os.path.basename(img_file)
            img_name = os.path.splitext(img_name)[0]
            fname = template_dir + img_name + '.dat'
            if os.path.exists(fname):
                continue
            if enhancement:
                rolled_template, enhanced_img = self.feature_extraction_single_rolled_enhancement(img_file)
                if template_dir is not None:
                    enhanced_img = np.asarray(enhanced_img, dtype=np.uint8)
                    io.imsave(os.path.join(template_dir, img_name + '.jpeg'), enhanced_img)
            else:
                rolled_template = self.feature_extraction_single_rolled(img_file, output_dir=template_dir)
            stop = timeit.default_timer()

            print stop - start
            if template_dir is not None:
                fname = template_dir + img_name + '.dat'
                print(fname)
                template.Template2Bin_Byte_TF_C(fname, rolled_template, isLatent=False)

    def feature_extraction_Longitudinal(self, image_dir, img_type='bmp', template_dir=None, enhancement=False,
                                        N1=0, N2=10000):

        subjects = os.listdir(image_dir)
        subjects.sort()

        assert(len(subjects) > 16000)

        subjects = subjects[N1:N2]
        for i, subject in enumerate(subjects):
            for finger_ID in range(10):
                img_files = glob.glob(os.path.join(image_dir, subject, '*' + str(finger_ID) + '.bmp'))
                img_files.sort()
                if len(img_files) < 5:
                    continue
                img_files = img_files[:5]
                for img_file in img_files:
                    start = timeit.default_timer()
                    img_name = os.path.basename(img_file)
                    img_name = os.path.splitext(img_name)[0]
                    if template_dir is not None:
                        fname = template_dir + subject + '_' + img_name + '.dat'
                        if os.path.exists(fname):
                            continue
                    if enhancement:
                        rolled_template, enhanced_img = self.feature_extraction_single_rolled_enhancement(img_file)
                        if template_dir is not None:
                            enhanced_img = np.asarray(enhanced_img, dtype=np.uint8)
                            io.imsave(os.path.join(template_dir, img_name + '.jpeg'), enhanced_img)
                    else:
                        rolled_template = self.feature_extraction_single_rolled(img_file, output_dir=template_dir)
                    stop = timeit.default_timer()

                    print stop - start
                    if template_dir is not None:
                        fname = template_dir + subject + '_' + img_name + '.dat'
                        print(fname)
                        template.Template2Bin_Byte_TF_C(fname, rolled_template, isLatent=False)

    def feature_extraction_MSP(self, image_dir, N1=0, N2=10000, template_dir=None, enhanced_img_path=None):

        assert(N2 - N1 > 0)
        assert(template_dir is not None)
        for i in range(N1, N2 + 1):
            start = timeit.default_timer()
            img_file = os.path.join(image_dir, str(i) + '.bmp')
            img_name = os.path.basename(img_file)
            fname = template_dir + os.path.splitext(img_name)[0] + '.dat'
            if os.path.exists(fname):
                continue
            rolled_template = self.feature_extraction_single_rolled(img_file, output_dir=template_dir)
            stop = timeit.default_timer()
            if rolled_template is not None:
                print(fname)
                template.Template2Bin_Byte_TF_C(fname, rolled_template, isLatent=True, save_mask=False)
                print stop - start
            else:
                print("rolled_template is None!")
                print stop - start

    def feature_extraction_N2N(self, image_dir, N1=0, N2=10000, template_dir=None, enhanced_img_path=None):

        subject_paths = glob.glob(image_dir + '*')
        assert (len(subject_paths) > 0)
        if not os.path.exists(template_dir):
            os.makedirs(template_dir)

        subject_paths = subject_paths[N1:N2]
        for subject_path in subject_paths:
            img_files = glob.glob(subject_path + '/*.png')
            assert (len(img_files) > 0)
            img_files.sort()
            for i, img_file in enumerate(img_files):
                print i, img_file
                img_name = os.path.basename(img_file)
                fname = template_dir + os.path.splitext(img_name)[0] + '.dat'
                if os.path.exists(fname):
                    continue

                rolled_template = self.feature_extraction_single_rolled(img_file, output_dir=template_dir, ppi=1200)
                stop = timeit.default_timer()
                if rolled_template is not None:
                    print(fname)
                    template.Template2Bin_Byte_TF_C(fname, rolled_template, isLatent=True, save_mask=False)

        assert(N2 - N1 > 0)
        assert(template_dir is not None)
        for i in range(N1, N2 + 1):
            start = timeit.default_timer()
            img_file = os.path.join(image_dir, str(i) + '.bmp')
            img_name = os.path.basename(img_file)
            fname = template_dir + os.path.splitext(img_name)[0] + '.dat'
            if os.path.exists(fname):
                continue
            rolled_template = self.feature_extraction_single_rolled(img_file, output_dir=template_dir)
            stop = timeit.default_timer()
            if rolled_template is not None:
                print(fname)
                template.Template2Bin_Byte_TF_C(fname, rolled_template, isLatent=True, save_mask=False)
            print stop - start


def parse_arguments(argv):
    parser = argparse.ArgumentParser()

    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.', default='0')
    parser.add_argument('--N1', type=int, help='rolled index from which the enrollment starts', default=0)
    parser.add_argument('--N2', type=int, help='rolled index from which the enrollment starts', default=2000)
    parser.add_argument('--tdir', type=str, help='data path for minutiae descriptor and minutiae extraction')
    parser.add_argument('--idir', type=str, help='data path for images')
    return parser.parse_args(argv)


if __name__ == '__main__':
    args = parse_arguments(sys.argv[1:])
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    pwd = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    with open(pwd + '/afis.config') as config_file:
        config = json.load(config_file)

    des_model_dirs = []
    patch_types = []
    model_dir = config['DescriptorModelPatch2']
    des_model_dirs.append(model_dir)
    patch_types.append(2)
    model_dir = config['DescriptorModelPatch8']
    des_model_dirs.append(model_dir)
    patch_types.append(8)
    model_dir = config['DescriptorModelPatch11']
    des_model_dirs.append(model_dir)
    patch_types.append(11)

    minu_model_dir = config['MinutiaeExtractionModel']

    LF_rolled = FeatureExtraction_Rolled(patch_types=patch_types, des_model_dirs=des_model_dirs,
                                         minu_model_dir=minu_model_dir)

    image_dir = args.idir if args.idir else config['GalleryImageDirectory']
    template_dir = args.tdir if args.tdir else config['GalleryTemplateDirectory']
    print("Starting feature extraction (batch)...")
    LF_rolled.feature_extraction(image_dir=image_dir, template_dir=template_dir, enhancement=False)
    print("Finished feature extraction. Starting dimensionality reduction...")
    descriptor_DR.template_compression(input_dir=template_dir, output_dir=template_dir,
                                       model_path=config['DimensionalityReductionModel'],
                                       isLatent=False, config=None)
    print("Finished dimensionality reduction. Starting product quantization...")
    descriptor_PQ.encode_PQ(input_dir=template_dir, output_dir=template_dir, fprint_type='rolled')
    print("Finished product quantization. Exiting...")
