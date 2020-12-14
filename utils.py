import torch
import tempfile
import torchaudio
from typing import List
import torch.nn as nn
import torch.nn.functional as F
from collections import deque
import numpy as np
from itertools import repeat
import onnxruntime

torchaudio.set_audio_backend("soundfile")  # switch backend

def read_audio(path: str,
               target_sr: int = 16000):

    assert torchaudio.get_audio_backend() == 'soundfile'
    wav, sr = torchaudio.load(path)

    if wav.size(0) > 1:
        wav = wav.mean(dim=0, keepdim=True)

    if sr != target_sr:
        transform = torchaudio.transforms.Resample(orig_freq=sr,
                                                   new_freq=target_sr)
        wav = transform(wav)
        sr = target_sr

    assert sr == target_sr
    return wav.squeeze(0)

def save_audio(path: str,
               tensor: torch.Tensor,
               sr: int):
    torchaudio.save(path, tensor, sr)


#def init_jit_model(model_url: str,
#                   device: torch.device = torch.device('cpu')):
#    torch.set_grad_enabled(False)
#    with tempfile.NamedTemporaryFile('wb', suffix='.model') as f:
#        torch.hub.download_url_to_file(model_url,
#                                       f.name,
#                                       progress=True)
#        model = torch.jit.load(f.name, map_location=device)
#        model.eval()
#    return model


def init_jit_model(model_path,
                   device):
    torch.set_grad_enabled(False)
    model = torch.jit.load(model_path, map_location=device)
    model.eval()
    return model

def init_onnx_model(model_path):
    return onnxruntime.InferenceSession(model_path)


def get_speech_ts(wav, model,
                  trig_sum=0.25, neg_trig_sum=0.01,
                  num_steps=8, batch_size=200):

    num_samples = 4000
    assert num_samples % num_steps == 0
    step = int(num_samples / num_steps)  # stride / hop
    outs = []
    to_concat = []

    for i in range(0, len(wav), step):
        chunk = wav[i: i+num_samples]
        if len(chunk) < num_samples:
            chunk = F.pad(chunk, (0, num_samples - len(chunk)))
        to_concat.append(chunk)
        if len(to_concat) >= batch_size:
            chunks = torch.Tensor(torch.vstack(to_concat))
            out = validate(model, chunks)[-2]
            outs.append(out)
            to_concat = []

    if to_concat:
        chunks = torch.Tensor(torch.vstack(to_concat))
        out = validate(model, chunks)[-2]
        outs.append(out)

    outs = torch.cat(outs, dim=0)

    buffer = deque(maxlen=num_steps)  # when max queue len is reach, first element is dropped
    triggered = False
    speeches = []
    current_speech = {}

    speech_probs = outs[:, 1]
    for i, predict in enumerate(speech_probs):  # add name
        buffer.append(predict)
        if (np.mean(buffer) >= trig_sum) and not triggered:
            triggered = True
            current_speech['start'] = step * max(0, i-num_steps)
        if (np.mean(buffer) < neg_trig_sum) and triggered:
            current_speech['end'] = step * i
            if (current_speech['end'] - current_speech['start']) > 10000:
                speeches.append(current_speech)
            current_speech = {}
            triggered = False
    if current_speech:
        current_speech['end'] = len(wav)
        speeches.append(current_speech)
    return speeches

class VADiterator:
    def __init__(self,
                 trig_sum=0.26, neg_trig_sum=0.01,
                 num_steps=8):
        self.num_samples = 4000
        self.num_steps = num_steps
        assert self.num_samples % num_steps == 0
        self.step = int(self.num_samples / num_steps)
        self.prev = torch.zeros(self.num_samples)
        self.last = False
        self.triggered = False
        self.buffer = deque(maxlen=num_steps)
        self.num_frames = 0
        self.trig_sum = trig_sum
        self.neg_trig_sum = neg_trig_sum
        self.current_name = ''

    def refresh(self):
        self.prev = torch.zeros(self.num_samples)
        self.last = False
        self.triggered = False
        self.buffer = deque(maxlen=self.num_steps)
        self.num_frames = 0

    def prepare_batch(self, wav_chunk, name=None):
        if (name is not None) and (name != self.current_name):
            self.refresh()
            self.current_name = name
        assert len(wav_chunk) <= self.num_samples
        self.num_frames += len(wav_chunk)
        if len(wav_chunk) < self.num_samples:
            wav_chunk = F.pad(wav_chunk, (0, self.num_samples - len(wav_chunk)))  # assume that short chunk means end of the audio
            self.last = True

        stacked = torch.hstack([self.prev, wav_chunk])
        self.prev = wav_chunk

        overlap_chunks = [stacked[i:i+self.num_samples] for i in range(self.step, self.num_samples+1, self.step)]  # 500 step is good enough
        return torch.vstack(overlap_chunks)

    def state(self, model_out):
        current_speech = {}
        speech_probs = model_out[:, 1]
        for i, predict in enumerate(speech_probs):  # add name
            self.buffer.append(predict)
            if (np.mean(self.buffer) >= self.trig_sum) and not self.triggered:
                self.triggered = True
                current_speech[self.num_frames - (self.num_steps-i) * self.step] = 'start'
            if (np.mean(self.buffer) < self.neg_trig_sum) and self.triggered:
                current_speech[self.num_frames - (self.num_steps-i) * self.step] = 'end'
                self.triggered = False
        if self.triggered and self.last:
            current_speech[self.num_frames] = 'end'
        if self.last:
            self.refresh()
        return current_speech, self.current_name


def state_generator(model, audios,
                    onnx=False,
                    trig_sum=0.26, neg_trig_sum=0.01,
                    num_steps=8, audios_in_stream=5):
    VADiters = [VADiterator(trig_sum, neg_trig_sum, num_steps) for i in range(audios_in_stream)]
    for i, current_pieces in enumerate(stream_imitator(audios, audios_in_stream)):
        for_batch = [x.prepare_batch(*y) for x, y in zip(VADiters, current_pieces)]
        batch = torch.cat(for_batch)

        outs = validate(model, batch)
        vad_outs = np.split(outs[-2].numpy(), audios_in_stream)

        states = []
        for x, y in zip(VADiters, vad_outs):
            cur_st = x.state(y)
            if cur_st[0]:
                states.append(cur_st)
        yield states


def stream_imitator(audios, audios_in_stream):
    audio_iter = iter(audios)
    iterators = []
    num_samples = 4000
    # initial wavs
    for i in range(audios_in_stream):
        next_wav = next(audio_iter)
        wav = read_audio(next_wav)
        wav_chunks = iter([(wav[i:i+num_samples], next_wav) for i in range(0, len(wav), num_samples)])
        iterators.append(wav_chunks)
    print('Done initial Loading')
    good_iters = audios_in_stream
    while True:
        values = []
        for i, it in enumerate(iterators):
            try:
                out, wav_name = next(it)
            except StopIteration:
                try:
                    next_wav = next(audio_iter)
                    print('Loading next wav: ', next_wav)
                    wav = read_audio(next_wav)
                    iterators[i] = iter([(wav[i:i+num_samples], next_wav) for i in range(0, len(wav), num_samples)])
                    out, wav_name = next(iterators[i])
                except StopIteration:
                    good_iters -= 1
                    iterators[i] = repeat((torch.zeros(num_samples), 'junk'))
                    out, wav_name = next(iterators[i])
                    if good_iters == 0:
                        return
            values.append((out, wav_name))
        yield values

def single_audio_stream(model, audio, onnx=False, trig_sum=0.26, 
                        neg_trig_sum=0.01, num_steps=8):
    num_samples = 4000
    VADiter = VADiterator(trig_sum, neg_trig_sum, num_steps)
    wav = read_audio(audio)
    wav_chunks = iter([wav[i:i+num_samples] for i in range(0, len(wav), num_samples)])
    for chunk in wav_chunks:
        batch = VADiter.prepare_batch(chunk)
        
        outs = validate(model, batch)
        vad_outs = outs[-2]

        states = []
        state = VADiter.state(vad_outs)
        if state[0]:
            states.append(state[0])
        yield states

def validate(model, inputs):
    onnx = False
    if type(model) == onnxruntime.capi.session.InferenceSession:
        onnx = True
    with torch.no_grad():
        if onnx:
            ort_inputs = {'input': inputs.cpu().numpy()}
            outs = model.run(None, ort_inputs)
            outs = [torch.Tensor(x) for x in outs]
        else:
            outs = model(inputs)
    return outs
