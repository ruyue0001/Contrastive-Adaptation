import torch
import argparse
import os
from pathlib import Path
import numpy as np
from data_process import process_small_data, myDataset
from model import Bertbaseline, BertContrastSequenceClassification
from torch.utils.data import DataLoader, SubsetRandomSampler
from transformers import BertModel, AdamW, get_scheduler, AutoModelForSequenceClassification, AutoTokenizer

import torch.nn.functional as F
from torch.nn.modules.loss import _Loss

from tqdm.auto import tqdm
from datasets import load_metric

parser = argparse.ArgumentParser(description='PyTorch BERT Text Classification')
parser.add_argument('--output_dir', type=str, default='./results',
                    help='location of the output dir')
parser.add_argument('--ckpt_dir', type=str, default='./checkpoints',
                    help='location of the checkpoint dir')
parser.add_argument('--task_type', type=str, default='in_domain',
                    help='task type, in_domain, single_source, multi_source, DA')
parser.add_argument('--dataset', type=str, default='amazon',
                    help='dataset name')
parser.add_argument('--num_bert', type=int, default=1,
                    help='num of bert')
parser.add_argument('--mask_percentage', type=float, default=0.1,
                    help='mask percentage')
parser.add_argument('--in_domain_loss', type=str, default='nce',
                    help='in domain loss, none, gv, jsd, nce')
parser.add_argument('--cross_domain_loss', type=str, default='nce',
                    help='cross domain loss')
parser.add_argument('--salient_model', type=str, default='gumble',
                    help='salient model, gumble, attn, descriptor, none')
parser.add_argument('--gpu', type=int, default=0,
                    help='gpu cuda visible devices')
parser.add_argument('--lr', type=float, default=1e-5,
                    help='initial learning rate')
parser.add_argument('--epochs', type=int, default=10,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=16, metavar='N',
                    help='batch size')
parser.add_argument('--sample_size', type=int, default=16, metavar='N',
                    help='sampling batch size')
parser.add_argument('--lbd', type=float, default=0.1,
                    help='labmda')
parser.add_argument('--lbd1', type=float, default=0.1,
                    help='labmda1')
parser.add_argument('--lbd2', type=float, default=0.1,
                    help='labmda2')
parser.add_argument('--tau', type=float, default=0.12,
                    help='contrastive temperature')
parser.add_argument('--load_from_pretrain', action='store_true',
                    help='if load from a pretrained domain classifier')
parser.add_argument('--max_length', type=int, default=512,
                    help='max length')

parser.add_argument('--adv_steps', type=int, default=1,
                    help="Number of gradient ascent steps for the adversary, should be at least 1")
parser.add_argument('--adv_init_mag', type=float, default=0,
                    help='Magnitude of initial (adversarial?) perturbation, 0, 1e-1, 2e-1, 3e-1, 5e-2, 8e-2')
parser.add_argument('--adv_noise_var', type=float, default=1e-5,
                    help='noise variance 1e-5')
parser.add_argument('--adv_lr', type=float, default=1e-4,
                    help='Step size of gradient ascent, 1e-1, 2e-1, 3e-2, 4e-2, 5e-2')
parser.add_argument('--adv_max_norm', type=float, default=1e-5,
                    help="Maximum norm of adversarial perturbation, set to 0 to be unlimited, 0, 7e-1")
parser.add_argument('--norm_type', type=str, default='linf',
                    help='linf or l2 or l1')
parser.add_argument('--adv_alpha', type=float, default=1,
                    help='virtual adversarial loss alpha')

args = parser.parse_args()

small_domain_names = ['book', 'electronics', 'beauty', 'music']

torch.cuda.set_device(args.gpu)


def stable_kl(logit, target, epsilon=1e-6, reduce=True):
    logit = logit.view(-1, logit.size(-1)).float()
    target = target.view(-1, target.size(-1)).float()
    bs = logit.size(0)
    p = F.log_softmax(logit, 1).exp()
    y = F.log_softmax(target, 1).exp()
    rp = -(1.0 / (p + epsilon) - 1 + epsilon).detach().log()
    ry = -(1.0 / (y + epsilon) - 1 + epsilon).detach().log()
    if reduce:
        return (p * (rp - ry) * 2).sum() / bs
    else:
        return (p * (rp - ry) * 2).sum()


class Criterion(_Loss):
    def __init__(self, alpha=1.0, name='criterion'):
        super().__init__()
        """Alpha is used to weight each loss term
        """
        self.alpha = alpha
        self.name = name

    def forward(self, input, target, weight=None, ignore_index=-1):
        """weight: sample weight
        """
        return

class SymKlCriterion(Criterion):
    def __init__(self, alpha=1.0, name='KL Div Criterion'):
        super().__init__()
        self.alpha = alpha
        self.name = name

    def forward(self, input, target, weight=None, ignore_index=-1, reduction='batchmean'):
        """input/target: logits
        """
        input = input.float()
        target = target.float()
        loss = F.kl_div(F.log_softmax(input, dim=-1, dtype=torch.float32), F.softmax(target.detach(), dim=-1, dtype=torch.float32), reduction=reduction) + \
            F.kl_div(F.log_softmax(target, dim=-1, dtype=torch.float32), F.softmax(input.detach(), dim=-1, dtype=torch.float32), reduction=reduction)
        loss = loss * self.alpha
        return loss

class JSCriterion(Criterion):
    def __init__(self, alpha=1.0, name='JS Div Criterion'):
        super().__init__()
        self.alpha = alpha
        self.name = name

    def forward(self, input, target, weight=None, ignore_index=-1, reduction='batchmean'):
        """input/target: logits
        """
        input = input.float()
        target = target.float()
        m = F.softmax(target.detach(), dim=-1, dtype=torch.float32) + \
            F.softmax(input.detach(), dim=-1, dtype=torch.float32)
        m = 0.5 * m
        loss = F.kl_div(F.log_softmax(input, dim=-1, dtype=torch.float32), m, reduction=reduction) + \
            F.kl_div(F.log_softmax(target, dim=-1, dtype=torch.float32), m, reduction=reduction)
        loss = loss * self.alpha
        return loss

def norm_grad(grad, eff_grad=None, sentence_level=False):
    if args.norm_type == 'l2':
        if sentence_level:
            direction = grad / (torch.norm(grad, dim=(-2, -1), keepdim=True) + args.adv_max_norm)
        else:
            direction = grad / (torch.norm(grad, dim=-1, keepdim=True) + args.adv_max_norm)
    elif args.norm_type == 'l1':
        direction = grad.sign()
    else:
        if sentence_level:
            direction = grad / (grad.abs().max((-2, -1), keepdim=True)[0] + args.adv_max_norm)
        else:
            direction = grad / (grad.abs().max(-1, keepdim=True)[0] + args.adv_max_norm)
            eff_direction = eff_grad / (grad.abs().max(-1, keepdim=True)[0] + args.adv_max_norm)
    return direction, eff_direction


def train_in_domain(domain_name):
    labeled_encodings, labeled_labels, train_encodings, train_labels, val_encodings, val_labels, unlabeled_encodings = process_small_data(
        domain_name)
    train_dataset = myDataset(train_encodings, train_labels)
    val_dataset = myDataset(val_encodings, val_labels)

    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    model = Bertbaseline(num_labels=3)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    test_loader = DataLoader(val_dataset, batch_size=args.batch_size)

    # ---optimizer---
    optimizer = AdamW(model.parameters(), lr=args.lr)
    # learning rate scheduler, using linear decay
    num_training_steps = args.epochs * len(train_loader)
    lr_scheduler = get_scheduler(
        "linear",
        optimizer=optimizer,
        num_warmup_steps=0.1 * num_training_steps,
        num_training_steps=num_training_steps
    )

    model.to(device)
    progress_bar = tqdm(range(num_training_steps))
    acc = 0
    # ---training---
    model.train()
    for epoch in range(args.epochs):
        for batch in train_loader:
            # optimizer.zero_grad()
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)  # [8, 512]
            labels = batch['labels'].to(device)
            # outputs = model(input_ids, attention_mask=attention_mask, labels=labels)


            #=========SMART adversarial training=========
            loss, logits, hidden_states, attentions = model(input_ids=input_ids, attention_mask=attention_mask,labels=labels)


            inputs = {"attention_mask": attention_mask, "labels": labels}

            embeds_init = model.bert.embeddings.word_embeddings(input_ids)

            noise = torch.zeros_like(embeds_init).normal_(0, 1) * args.adv_noise_var
            noise.detach()
            noise.requires_grad_()

            for step in range(args.adv_steps):
                inputs['inputs_embeds'] = noise + embeds_init
                adv_loss, adv_logits, hidden_states, attentions = model(
                    inputs_embeds=inputs['inputs_embeds'],
                    attention_mask=inputs['attention_mask'],
                    labels=inputs['labels']
                )

                # (1) calc the adversarial loss - KL divergence
                #adv_logits = adv_logits.view(-1, 1)
                adv_loss = stable_kl(adv_logits, logits.detach(), reduce=False)

                # (2) calc the gradient for the noise
                delta_grad, = torch.autograd.grad(adv_loss, noise, only_inputs=True, retain_graph=False)

                norm = delta_grad.norm()
                if (torch.isnan(norm) or torch.isinf(norm)):
                    return 0

                delta_grad_clone = delta_grad.clone().detach()
                denorm = torch.norm(delta_grad_clone.view(delta_grad_clone.size(0), -1), dim=1, p=float("inf")).view(-1, 1, 1)
                denorm = torch.clamp(denorm, min=1e-8)
                noise = (noise + args.adv_lr * delta_grad_clone / denorm).detach()
                if args.adv_max_norm > 0:
                    noise = torch.clamp(noise, -args.adv_max_norm, args.adv_max_norm).detach()

                noise.requires_grad_()

                '''
                eff_delta_grad = delta_grad * args.adv_lr
                delta_grad = noise + delta_grad * args.adv_lr
                noise, eff_noise = norm_grad(delta_grad, eff_grad=eff_delta_grad, sentence_level=False)
                noise = noise.detach()
                noise.requires_grad_()
                '''
            inputs['inputs_embeds'] = noise + embeds_init
            adv_loss, adv_logits, hidden_states, attentions = model(
                inputs_embeds=inputs['inputs_embeds'],
                attention_mask=inputs['attention_mask'],
                labels=inputs['labels']
            )
            adv_lf = SymKlCriterion()
            adv_loss = adv_lf(logits, adv_logits)

            loss = loss + args.adv_alpha * adv_loss
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            progress_bar.update(1)

            '''
            # =========virtual adversarial training========
            loss, logits, hidden_states, attentions = model(input_ids=input_ids, attention_mask=attention_mask,labels=labels)

            inputs = {"attention_mask": attention_mask, "labels": labels}

            embeds_init = model.bert.embeddings.word_embeddings(input_ids)

            # (1) initialize
            delta = torch.zeros_like(embeds_init).normal_(0, 1) * args.adv_noise_var
            delta = delta.detach()
            delta.requires_grad_()

            for step in range(args.adv_steps):
                inputs['inputs_embeds'] = delta + embeds_init

                # (2) calc the adversarial loss
                adv_loss, adv_logits, adv_hidden_states, adv_attentions = model(
                    inputs_embeds=inputs['inputs_embeds'],
                    attention_mask=inputs['attention_mask'],
                    labels=inputs['labels']
                )

                # (3) calc the gradient for the noise
                delta_grad, = torch.autograd.grad(adv_loss, delta, only_inputs=True, retain_graph=False)
        
                # (4) projected gradient decent
                if args.norm_type == "linf":
                    denorm = torch.norm(delta_grad.view(delta_grad.size(0), -1), dim=1, p=float("inf")).view(-1, 1, 1)

                    delta = (delta + args.adv_lr * delta_grad / denorm).detach()
                    if args.adv_max_norm > 0:
                        delta = torch.clamp(delta, -args.adv_max_norm, args.adv_max_norm).detach()

                delta = delta.detach()
                delta.requires_grad_()

            # (5) update the parameters
            inputs['inputs_embeds'] = delta + embeds_init
            adv_loss, adv_logits, adv_hidden_states, adv_attentions = model(
                inputs_embeds=inputs['inputs_embeds'],
                attention_mask=inputs['attention_mask'],
                labels=inputs['labels']
            )
            adv_loss = stable_kl(adv_logits, logits, reduce=False)
            loss = loss + args.adv_alpha * adv_loss
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            progress_bar.update(1)

            '''
            '''
            # =========freeLB adversarial training========
            inputs = {"attention_mask": attention_mask, "labels": labels}

            embeds_init = model.bert.embeddings.word_embeddings(input_ids)

            if args.adv_init_mag > 0:
                input_mask = inputs['attention_mask'].to(embeds_init)
                input_lengths = torch.sum(input_mask, 1)

                if args.norm_type == "l2":
                    delta = torch.zeros_like(embeds_init).uniform_(-1, 1) * input_mask.unsqueeze(2)
                    dims = input_lengths * embeds_init.size(-1)
                    mag = args.adv_init_mag / torch.sqrt(dims)
                    delta = (delta * mag.view(-1, 1, 1)).detach()
                elif args.norm_type == "linf":
                    delta = torch.zeros_like(embeds_init).uniform_(-args.adv_init_mag,
                                                                   args.adv_init_mag) * input_mask.unsqueeze(2)

            elif args.adv_noise_var > 0:
                input_mask = inputs['attention_mask'].to(embeds_init)
                delta = torch.zeros_like(embeds_init).normal_(0, 1) * args.adv_noise_var
                delta = delta * input_mask.unsqueeze(2)

            else:
                delta = torch.zeros_like(embeds_init)

            # the main loop
            for astep in range(args.adv_steps):
                # (0) forward
                delta.requires_grad_()
                inputs['inputs_embeds'] = delta + embeds_init
                loss, logits, hidden_states, attentions = model(
                    inputs_embeds=inputs['inputs_embeds'],
                    attention_mask=inputs['attention_mask'],
                    labels=inputs['labels']
                )

                # (1) backward
                loss = loss / args.adv_steps

                loss.backward()

                if astep == args.adv_steps - 1:
                    break

                # (2) get gradient on delta
                delta_grad = delta.grad.clone().detach()

                # (3) update and clip
                if args.norm_type == "l2":
                    denorm = torch.norm(delta_grad.view(delta_grad.size(0), -1), dim=1).view(-1, 1, 1)
                    denorm = torch.clamp(denorm, min=1e-8)
                    delta = (delta + args.adv_lr * delta_grad / denorm).detach()
                    if args.adv_max_norm > 0:
                        delta_norm = torch.norm(delta.view(delta.size(0), -1).float(), p=2, dim=1).detach()
                        exceed_mask = (delta_norm > args.adv_max_norm).to(embeds_init)
                        reweights = (args.adv_max_norm / delta_norm * exceed_mask + (1 - exceed_mask)).view(-1, 1, 1)
                        delta = (delta * reweights).detach()
                elif args.norm_type == "linf":
                    denorm = torch.norm(delta_grad.view(delta_grad.size(0), -1), dim=1, p=float("inf")).view(-1, 1, 1)
                    denorm = torch.clamp(denorm, min=1e-8)
                    delta = (delta + args.adv_lr * delta_grad / denorm).detach()
                    if args.adv_max_norm > 0:
                        delta = torch.clamp(delta, -args.adv_max_norm, args.adv_max_norm).detach()
                else:
                    print("Norm type {} not specified.".format(args.norm_type))
                    exit()

                embeds_init = model.bert.embeddings.word_embeddings(input_ids)

            # loss, logits, hidden_states, attentions = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)

            # loss.backward()

            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            progress_bar.update(1)
            '''
        # ---evaluation---
        metric = load_metric("accuracy")
        model.eval()
        for batch in test_loader:
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = batch['labels'].to(device)
            with torch.no_grad():
                loss, logits, hidden_states, attentions = model(input_ids=input_ids, attention_mask=attention_mask,
                                                                labels=labels)

            predictions = torch.argmax(logits, dim=-1)
            metric.add_batch(predictions=predictions, references=batch["labels"])

        score = metric.compute()

        print(domain_name, score)

        if score['accuracy'] >= acc:
            acc = score['accuracy']
            checkpoint_path = args.ckpt_dir + "/" + domain_name + ".ckpt"
            torch.save(model.state_dict(), checkpoint_path)

    return


def train_single_source(source_domain_name, target_domain_name):
    s_labeled_encodings, s_labeled_labels, s_train_encodings, s_train_labels, s_val_encodings, s_val_labels, s_unlabeled_encodings = process_small_data(
        source_domain_name, max_length=args.max_length)
    s_train_dataset = myDataset(s_train_encodings, s_train_labels)
    s_val_dataset = myDataset(s_val_encodings, s_val_labels)

    t_labeled_encodings, t_labeled_labels, t_train_encodings, t_train_labels, t_val_encodings, t_val_labels, t_unlabeled_encodings = process_small_data(
        target_domain_name, max_length=args.max_length)
    t_labeled_dataset = myDataset(t_labeled_encodings, t_labeled_labels)

    source_train_loader = DataLoader(s_train_dataset, batch_size=args.batch_size, shuffle=True)
    source_val_loader = DataLoader(s_val_dataset, batch_size=args.batch_size)
    target_test_loader = DataLoader(t_labeled_dataset, batch_size=args.batch_size)

    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    model = Bertbaseline(num_labels=3)

    # ---optimizer---
    # optimizer = AdamW(model.parameters(), lr=args.lr, weight_decay=1e-5, correct_bias=False)
    # optimizer = AdamW(model.parameters(), lr=args.lr, correct_bias=False)
    optimizer = AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    # optimizer = AdamW(model.parameters(), lr=args.lr)
    # learning rate scheduler, using linear decay
    num_training_steps = args.epochs * len(source_train_loader)
    lr_scheduler = get_scheduler(
        "linear",
        optimizer=optimizer,
        num_warmup_steps=0.1 * num_training_steps,
        num_training_steps=num_training_steps
    )

    model.to(device)
    progress_bar = tqdm(range(num_training_steps))
    s_acc = 0
    t_acc = 0
    tr_loss, logging_loss = 0.0, 0.0
    # ---training---
    model.train()
    for epoch in range(args.epochs):
        for batch in source_train_loader:
            # optimizer.zero_grad()
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)  # [8, 128]
            labels = batch['labels'].to(device)
            # outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
            # loss = outputs.loss

            # =========adversarial training========
            inputs = {"attention_mask": attention_mask, "labels": labels}

            embeds_init = model.bert.embeddings.word_embeddings(input_ids)

            if args.adv_init_mag > 0:
                input_mask = inputs['attention_mask'].to(embeds_init)
                input_lengths = torch.sum(input_mask, 1)

                if args.norm_type == "l2":
                    delta = torch.zeros_like(embeds_init).uniform_(-1, 1) * input_mask.unsqueeze(2)
                    dims = input_lengths * embeds_init.size(-1)
                    mag = args.adv_init_mag / torch.sqrt(dims)
                    delta = (delta * mag.view(-1, 1, 1)).detach()
                elif args.norm_type == "linf":
                    delta = torch.zeros_like(embeds_init).uniform_(-args.adv_init_mag,
                                                                   args.adv_init_mag) * input_mask.unsqueeze(2)
            else:
                delta = torch.zeros_like(embeds_init)

            # the main loop
            for astep in range(args.adv_steps):
                # (0) forward
                delta.requires_grad_()
                inputs['inputs_embeds'] = delta + embeds_init
                loss, logits, hidden_states, attentions = model(
                    inputs_embeds=inputs['inputs_embeds'],
                    attention_mask=inputs['attention_mask'],
                    labels=inputs['labels']
                )

                # (1) backward
                loss = loss / args.adv_steps
                tr_loss += loss.item()

                loss.backward()

                if astep == args.adv_steps - 1:
                    break

                # (2) get gradient on delta
                delta_grad = delta.grad.clone().detach()

                # (3) update and clip
                if args.norm_type == "l2":
                    denorm = torch.norm(delta_grad.view(delta_grad.size(0), -1), dim=1).view(-1, 1, 1)
                    denorm = torch.clamp(denorm, min=1e-8)
                    delta = (delta + args.adv_lr * delta_grad / denorm).detach()
                    if args.adv_max_norm > 0:
                        delta_norm = torch.norm(delta.view(delta.size(0), -1).float(), p=2, dim=1).detach()
                        exceed_mask = (delta_norm > args.adv_max_norm).to(embeds_init)
                        reweights = (args.adv_max_norm / delta_norm * exceed_mask + (1 - exceed_mask)).view(-1, 1, 1)
                        delta = (delta * reweights).detach()
                elif args.norm_type == "linf":
                    denorm = torch.norm(delta_grad.view(delta_grad.size(0), -1), dim=1, p=float("inf")).view(-1, 1, 1)
                    denorm = torch.clamp(denorm, min=1e-8)
                    delta = (delta + args.adv_lr * delta_grad / denorm).detach()
                    if args.adv_max_norm > 0:
                        delta = torch.clamp(delta, -args.adv_max_norm, args.adv_max_norm).detach()
                else:
                    print("Norm type {} not specified.".format(args.norm_type))
                    exit()

                embeds_init = model.bert.embeddings.word_embeddings(input_ids)

            # loss, logits, hidden_states, attentions = model(input_ids,attention_mask=attention_mask, labels=labels)

            # loss.backward()

            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            progress_bar.update(1)

        # ----------validation----------
        metric_val = load_metric("accuracy")
        model.eval()

        for batch in source_val_loader:
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = batch['labels'].to(device)
            with torch.no_grad():
                loss, logits, hidden_states, attentions = model(input_ids, attention_mask=attention_mask, labels=labels)
                # outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
                # logits = outputs.logits

            predictions = torch.argmax(logits, dim=-1)
            metric_val.add_batch(predictions=predictions, references=batch["labels"])

        s_score = metric_val.compute()

        # ----------testing----------
        metric_test = load_metric("accuracy")
        model.eval()

        for batch in target_test_loader:
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = batch['labels'].to(device)
            with torch.no_grad():
                loss, logits, hidden_states, attentions = model(input_ids, attention_mask=attention_mask, labels=labels)
                # outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
                # logits = outputs.logits

            predictions = torch.argmax(logits, dim=-1)
            metric_test.add_batch(predictions=predictions, references=batch["labels"])

        t_score = metric_test.compute()

        print(source_domain_name, target_domain_name, s_score, t_score)

        if s_score['accuracy'] >= s_acc:
            s_acc = s_score['accuracy']
            t_acc = t_score['accuracy']
            checkpoint_path = args.ckpt_dir + "/" + source_domain_name + "-" + target_domain_name + ".ckpt"
            torch.save(model.state_dict(), checkpoint_path)

    print(s_acc, t_acc)
    return t_acc


if __name__ == "__main__":
    train_in_domain('book')

    # train_single_source('electronics', 'book')
    '''
    acc = []
    for i in range(5):
        for target_domain_name in small_domain_names:
            for source_domain_name in small_domain_names:
                if target_domain_name != source_domain_name:
                    t_acc = train_single_source(source_domain_name, target_domain_name)
                    acc.append(t_acc)
    print (acc)
    '''