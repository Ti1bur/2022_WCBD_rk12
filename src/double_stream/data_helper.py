import os
import json
import zipfile
import random
import zipfile
import torch
import numpy as np
from PIL import Image
from io import BytesIO
from functools import partial
from transformers import BertTokenizer
from torch.utils.data import Dataset
from torch.utils.data import DataLoader, Dataset, RandomSampler, SequentialSampler
from torchvision.transforms import Compose, Resize, CenterCrop, Normalize, ToTensor

from category_id_map import category_id_to_lv2id


def create_dataloaders(args):
    # train_dataset = MultiModalDataset(args, '/home/tione/notebook/data/annotations/fold10/1train.json', '/home/tione/notebook/data/zip_feats/labeled_vit.zip')
    # val_dataset = MultiModalDataset(args, '/home/tione/notebook/data/annotations/fold10/1valid.json', '/home/tione/notebook/data/zip_feats/labeled_vit.zip')
    dataset = MultiModalDataset(args, args.train_annotation, args.train_zip_frames)
    size = len(dataset)
    val_size = int(size * args.val_ratio)
    train_dataset, val_dataset = torch.utils.data.random_split(dataset, [size - val_size, val_size],
                                                               generator=torch.Generator().manual_seed(args.seed))

    if args.num_workers > 0:
        dataloader_class = partial(DataLoader, pin_memory=True, num_workers=args.num_workers, prefetch_factor=args.prefetch)
    else:
        # single-thread reading does not support prefetch_factor arg
        dataloader_class = partial(DataLoader, pin_memory=True, num_workers=0)

    if args.ispretrain:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = SequentialSampler(val_dataset)
        # shuffle=False,
        train_dataloader = dataloader_class(train_dataset,
                                            shuffle=False,
                                            batch_size=args.batch_size,
                                            sampler=train_sampler,
                                            drop_last=True)
        val_dataloader = dataloader_class(val_dataset,
                                          batch_size=args.val_batch_size,
                                          sampler=val_sampler,
                                          drop_last=False)
        return train_dataloader, val_dataloader, train_sampler
    else:
        train_sampler = RandomSampler(train_dataset)
        val_sampler = SequentialSampler(val_dataset)
        # shuffle=False,
        train_dataloader = dataloader_class(train_dataset,
                                            batch_size=args.batch_size,
                                            sampler=train_sampler,
                                            drop_last=True)
        val_dataloader = dataloader_class(val_dataset,
                                          batch_size=args.val_batch_size,
                                          sampler=val_sampler,
                                          drop_last=False)
        return train_dataloader, val_dataloader


class MultiModalDataset(Dataset):
    """ A simple class that supports multi-modal inputs.
    For the visual features, this dataset class will read the pre-extracted
    features from the .npy files. For the title information, it
    uses the BERT tokenizer to tokenize. We simply ignore the ASR & OCR text in this implementation.
    Args:
        ann_path (str): annotation file path, with the '.json' suffix.
        zip_feats (str): visual feature zip file path.
        test_mode (bool): if it's for testing.
    """

    def __init__(self,
                 args,
                 ann_path: str,
                 zip_feats: str,
                 test_mode: bool = False):
        self.max_frame = args.max_frames               # 最大帧数 
        self.bert_seq_length = args.bert_seq_length    # 最大句长 
        self.test_mode = test_mode                     # 是否是预测模式
        self.ispretrain = args.ispretrain

        self.zip_feat_path = zip_feats                 # feat路径
        self.num_workers = args.num_workers            # 线程数
        if self.num_workers > 0:
            # zip_handler的懒惰初始化，避免多进程读取错误
            self.handles = [None for _ in range(args.num_workers)]
        else:
            self.handles = zipfile.ZipFile(self.zip_feat_path, 'r')
        # 加载文本信息
        with open(ann_path, 'r', encoding='utf8') as f:
            self.anns = json.load(f)
        # 初始化文本分词器
        self.tokenizer = BertTokenizer.from_pretrained(args.bert_dir, use_fast=True, cache_dir=args.bert_cache)

    def __len__(self) -> int:
        return len(self.anns)

    def get_visual_feats(self, idx: int) -> tuple:
        # read data from zipfile
        vid = self.anns[idx]['id']
        if self.num_workers > 0:
            worker_id = torch.utils.data.get_worker_info().id
            if self.handles[worker_id] is None:
                self.handles[worker_id] = zipfile.ZipFile(self.zip_feat_path, 'r')
            handle = self.handles[worker_id]
        else:
            handle = self.handles
        raw_feats = np.load(BytesIO(handle.read(name=f'{vid}.npy')), allow_pickle=True)
        raw_feats = raw_feats.astype(np.float32)  # float16 to float32
        num_frames, feat_dim = raw_feats.shape

        feat = np.zeros((self.max_frame, feat_dim), dtype=np.float32)
        mask = np.ones((self.max_frame,), dtype=np.int32)
        if num_frames <= self.max_frame:
            feat[:num_frames] = raw_feats
            mask[num_frames:] = 0
        else:
            # if the number of frames exceeds the limitation, we need to sample
            # the frames.
            # if self.test_mode:
            #     # uniformly sample when test mode is True
            #     step = num_frames // self.max_frame
            #     select_inds = list(range(0, num_frames, step))
            #     select_inds = select_inds[:self.max_frame]
            # else:
            #     # randomly sample when test mode is False
            #     select_inds = list(range(num_frames))
            #     random.shuffle(select_inds)
            #     select_inds = select_inds[:self.max_frame]
            #     select_inds = sorted(select_inds)
            # randomly sample when test mode is False
            select_inds = list(range(num_frames))
            random.shuffle(select_inds)
            select_inds = select_inds[:self.max_frame]
            select_inds = sorted(select_inds)
            for i, j in enumerate(select_inds):
                feat[i] = raw_feats[j]
        feat = torch.FloatTensor(feat)
        mask = torch.LongTensor(mask)
        return feat, mask

    def tokenize_text(self, text: str) -> tuple:
        encoded_inputs = self.tokenizer(text, max_length=self.bert_seq_length, padding='max_length', truncation=True)
        input_ids = torch.LongTensor(encoded_inputs['input_ids'])
        mask = torch.LongTensor(encoded_inputs['attention_mask'])
        return input_ids, mask

    def __getitem__(self, idx: int) -> dict:
        # Step 1, load visual features from zipfile.
        frame_input, frame_mask = self.get_visual_feats(idx)

        # Step 2, load title tokens
        text = self.anns[idx]['title'] + self.anns[idx]['asr']
        for a in self.anns[idx]['ocr']:
            text += a['text']
        title_input, title_mask = self.tokenize_text(text)

        # Step 3, summarize into a dictionary
        data = dict(
            frame_input=frame_input,
            frame_mask=frame_mask,
            title_input=title_input,
            title_mask=title_mask
        )

        # Step 4, load label if not test mode
        if not self.test_mode:
            if not self.ispretrain:
                label = category_id_to_lv2id(self.anns[idx]['category_id'])
                data['label'] = torch.LongTensor([label])

        return data

# class MultiModalDataset(Dataset):
#     """ A simple class that supports multi-modal inputs.

#     Args:
#         ann_path (str): annotation file path, with the '.json' suffix.
#         zip_frame_dir (str): visual frame zip file path.
#         test_mode (bool): if it's for testing.

#     """

#     def __init__(self,
#                  args,
#                  ann_path: str,
#                  zip_frame_dir: str,
#                  test_mode: bool = False):
#         self.max_frame = args.max_frames
#         self.bert_seq_length = args.bert_seq_length
#         self.test_mode = test_mode

#         self.zip_frame_dir = zip_frame_dir
#         # load annotations
#         with open(ann_path, 'r', encoding='utf8') as f:
#             self.anns = json.load(f)
#         # initialize the text tokenizer
#         self.tokenizer = BertTokenizer.from_pretrained(args.bert_dir, use_fast=True, cache_dir=args.bert_cache)

#         # we use the standard image transform as in the offifical Swin-Transformer.
#         self.transform = Compose([
#             Resize(256),
#             CenterCrop(224),
#             ToTensor(),
#             Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
#         ])

#     def __len__(self) -> int:
#         return len(self.anns)

#     def get_visual_frames(self, idx: int) -> tuple:
#         # read data from zipfile
#         vid = self.anns[idx]['id']
#         zip_path = os.path.join(self.zip_frame_dir, f'{vid[-3:]}/{vid}.zip')
#         handler = zipfile.ZipFile(zip_path, 'r')
#         namelist = sorted(handler.namelist())

#         num_frames = len(namelist)
#         frame = torch.zeros((self.max_frame, 3, 224, 224), dtype=torch.float32)
#         mask = torch.zeros((self.max_frame, ), dtype=torch.long)
#         if num_frames <= self.max_frame:
#             # load all frame
#             select_inds = list(range(num_frames))
#         else:
#             # if the number of frames exceeds the limitation, we need to sample
#             # the frames.
#             # if self.test_mode:
#             #     # uniformly sample when test mode is True
#             #     step = num_frames // self.max_frame
#             #     select_inds = list(range(0, num_frames, step))
#             #     select_inds = select_inds[:self.max_frame]
#             # else:
#             #     # randomly sample when test mode is False
#             #     select_inds = list(range(num_frames))
#             #     random.shuffle(select_inds)
#             #     select_inds = select_inds[:self.max_frame]
#             #     select_inds = sorted(select_inds)
#             # randomly sample when test mode is False
#             select_inds = list(range(num_frames))
#             random.shuffle(select_inds)
#             select_inds = select_inds[:self.max_frame]
#             select_inds = sorted(select_inds)
#         for i, j in enumerate(select_inds):
#             mask[i] = 1
#             img_content = handler.read(namelist[j])
#             img = Image.open(BytesIO(img_content))
#             img_tensor = self.transform(img)
#             frame[i] = img_tensor
#         return frame, mask

#     def tokenize_text(self, text: str) -> tuple:
#         encoded_inputs = self.tokenizer(text, max_length=self.bert_seq_length, padding='max_length', truncation=True)
#         input_ids = torch.LongTensor(encoded_inputs['input_ids'])
#         mask = torch.LongTensor(encoded_inputs['attention_mask'])
#         return input_ids, mask

#     def __getitem__(self, idx: int) -> dict:
#         # Step 1, load visual features from zipfile.
#         frame_input, frame_mask = self.get_visual_frames(idx)

#         # Step 2, load title tokens
#         text = self.anns[idx]['title'] + self.anns[idx]['asr']
#         for a in self.anns[idx]['ocr']:
#             text += a['text']
#         title_input, title_mask = self.tokenize_text(text)

#         # Step 3, summarize into a dictionary
#         data = dict(
#             frame_input=frame_input,
#             frame_mask=frame_mask,
#             title_input=title_input,
#             title_mask=title_mask
#         )

#         # Step 4, load label if not test mode
#         if not self.test_mode:
#             label = category_id_to_lv2id(self.anns[idx]['category_id'])
#             data['label'] = torch.LongTensor([label])

#         return data
