import csv
import hashlib
import shutil

import math
import os
import random
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

import librosa
import numpy as np
import scipy.ndimage
import scipy.signal
import torch
import torchaudio
import tqdm
from torch.distributed import get_rank
from torch.distributed import get_world_size
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from torch.utils.data.sampler import Sampler

from data.curriculum import Curriculum
from data.labels import Labels

tq = tqdm.tqdm

windows = {'hamming': scipy.signal.hamming,
           'hann': scipy.signal.hann,
           'blackman': scipy.signal.blackman,
           'bartlett': scipy.signal.bartlett}


def load_audio(path, channel=-1):
    sound, sample_rate = torchaudio.load(path, normalization=False)
    sound = sound.numpy().T
    if len(sound.shape) > 1:
        if sound.shape[1] == 1:
            sound = sound.squeeze()
        elif channel == -1:
            sound = sound.mean(axis=1)  # multiple channels, average
        else:
            sound = sound[:, channel]  # multiple channels, average
    return sound, sample_rate


class AudioParser(object):
    def parse_transcript(self, transcript_path):
        """
        :param transcript_path: Path where transcript is stored from the manifest file
        :return: Transcript in training/testing format
        """
        raise NotImplementedError

    def parse_audio(self, audio_path):
        """
        :param audio_path: Path where audio is stored from the manifest file
        :return: Audio in training/testing format
        """
        raise NotImplementedError


class NoiseInjection(object):
    def __init__(self,
                 path=None,
                 sample_rate=16000,
                 noise_levels=(0, 0.5)):
        """
        Adds noise to an input signal with specific SNR. Higher the noise level, the more noise added.
        Modified code from https://github.com/willfrey/audio/blob/master/torchaudio/transforms.py
        """
        if not os.path.exists(path):
            print("Directory doesn't exist: {}".format(path))
            raise IOError
        self.paths = path is not None and librosa.util.find_files(path)
        self.sample_rate = sample_rate
        self.noise_levels = noise_levels

    def inject_noise(self, data):
        noise_path = np.random.choice(self.paths)
        noise_level = np.random.uniform(*self.noise_levels)
        return self.inject_noise_sample(data, noise_path, noise_level)

    def inject_noise_sample(self, data, noise_path, noise_level):
        noise_len = get_audio_length(noise_path)
        data_len = len(data) / self.sample_rate
        noise_start = np.random.rand() * (noise_len - data_len)
        noise_end = noise_start + data_len
        noise_dst, sample_rate_ = audio_with_sox(noise_path, self.sample_rate, noise_start, noise_end)
        assert sample_rate_ == self.sample_rate
        assert len(data) == len(noise_dst)
        noise_energy = np.sqrt(noise_dst.dot(noise_dst)) / noise_dst.size
        data_energy = np.sqrt(data.dot(data)) / data.size
        data += noise_level * noise_dst * data_energy / noise_energy
        return data


TEMPOS = {
    0: ('1.0', (1.0, 1.0)),
    1: ('0.9', (0.85, 0.95)),
    2: ('1.1', (1.05, 1.15))
}


class SpectrogramParser(AudioParser):
    def __init__(self, audio_conf, cache_path, normalize=False, augment=False, channel=-1):
        """
        Parses audio file into spectrogram with optional normalization and various augmentations
        :param audio_conf: Dictionary containing the sample rate, window and the window length/stride in seconds
        :param normalize(default False):  Apply standard mean and deviation normalization to audio tensor
        :param augment(default False):  Apply random tempo and gain perturbations
        """
        super(SpectrogramParser, self).__init__()
        self.window_stride = audio_conf['window_stride']
        self.window_size = audio_conf['window_size']
        self.sample_rate = audio_conf['sample_rate']
        self.window = windows.get(audio_conf['window'], windows['hamming'])
        self.normalize = normalize
        self.augment = augment
        self.channel = channel
        self.cache_path = cache_path
        self.noiseInjector = NoiseInjection(audio_conf['noise_dir'], self.sample_rate,
                                            audio_conf['noise_levels']) if audio_conf.get(
            'noise_dir') is not None else None
        self.noise_prob = audio_conf.get('noise_prob')

    def load_audio_cache(self, audio_path, tempo_id):
        tempo_name, tempo = TEMPOS[tempo_id]
        chan = 'avg' if self.channel == -1 else str(self.channel)
        f_path = Path(audio_path)
        f_hash = hashlib.sha1(f_path.read_bytes()).hexdigest()[:9]
        cache_fn = Path(self.cache_path, f_hash[:2],
                        f_path.name + '.' + f_hash[2:] + '.' + tempo_name + '.' + chan + '.npy')
        cache_fn.parent.mkdir(parents=True, exist_ok=True)
        old_cache_fn = audio_path + '-' + tempo_name + '-' + chan + '.npy'
        if os.path.exists(old_cache_fn) and not os.path.exists(cache_fn):
            print(f"Moving {old_cache_fn} to {cache_fn}")
            shutil.move(old_cache_fn, cache_fn)
        spec = None
        if os.path.exists(cache_fn):
            # print("Loading", cache_fn)
            try:
                spec = np.load(cache_fn).item()['spect']
            except Exception as e:
                import traceback
                print("Can't load file", cache_fn, 'with exception:', str(e))
                traceback.print_exc()
        return cache_fn, spec

    def parse_audio(self, audio_path):
        if self.augment:
            tempo_id = random.randrange(3)
        else:
            tempo_id = 0
        cache_fn, spect = self.load_audio_cache(audio_path, tempo_id)

        # FIXME: If one needs to reset cache
        # spect = None

        if spect is None:
            if self.augment or True:
                y, sample_rate = load_randomly_augmented_audio(audio_path, self.sample_rate,
                                                               channel=self.channel, tempo_range=TEMPOS[tempo_id][1])
            else:
                # FIXME: We never call this
                y, sample_rate = load_audio(audio_path, channel=self.channel)
            if self.noiseInjector:
                add_noise = np.random.binomial(1, self.noise_prob)
                if add_noise:
                    y = self.noiseInjector.inject_noise(y)

            spect = self.audio_to_stft(y, sample_rate)
            spect = self.normalize_audio(spect)

            # FIXME: save to the file, but only if it's for
            if tempo_id == 0:
                try:
                    np.save(str(cache_fn) + '.tmp.npy', {'spect': spect})
                    os.rename(str(cache_fn) + '.tmp.npy', cache_fn)
                    # print("Saved to", cache_fn)
                except KeyboardInterrupt:
                    os.unlink(cache_fn)

        if self.augment and self.normalize == 'max_frame':
            spect.add_(torch.rand(1) - 0.5)
        return spect

    def parse_audio_for_transcription(self, audio_path):
        return self.parse_audio(audio_path)

    def audio_to_stft(self, y, sample_rate):
        n_fft = int(sample_rate * (self.window_size + 1e-8))
        win_length = n_fft
        hop_length = int(sample_rate * (self.window_stride + 1e-8))
        # print(n_fft, win_length, hop_length)
        # STFT
        D = librosa.stft(y, n_fft=n_fft, hop_length=hop_length,
                         win_length=win_length, window=self.window)
        spect, phase = librosa.magphase(D)
        shape = spect.shape
        if shape[0] < 161:
            spect.resize((161, *shape[1:]))
            spect[81:] = spect[80:0:-1]
        # print(spect.shape)
        # print(shape, spect.shape)
        return spect[:161]

    def normalize_audio(self, spect):
        # S = log(S+1)
        if self.normalize == 'mean':
            spect = np.log1p(spect)
            spect = torch.FloatTensor(spect)
            mean = spect.mean()
            spect.add_(-mean)
        elif self.normalize == 'norm':
            spect = np.log1p(spect)
            spect = torch.FloatTensor(spect)
            mean = spect.mean()
            spect.add_(-mean)
            std = spect.std(dim=0, keepdim=True)
            spect.div_(std.mean())
        elif self.normalize == 'frame':
            spect = np.log1p(spect)
            spect = torch.FloatTensor(spect)
            mean = spect.mean(dim=0, keepdim=True)
            # std = spect.std(dim=0, keepdim=True)
            mean = torch.FloatTensor(scipy.ndimage.filters.gaussian_filter1d(mean.numpy(), 50))
            # std = torch.FloatTensor(scipy.ndimage.filters.gaussian_filter1d(std.numpy(), 20))
            spect.add_(-mean.mean())
            # spect.div_(std.mean() + 1e-8)
        elif self.normalize == 'max_frame':
            spect = np.log1p(spect * 1048576)
            spect = torch.FloatTensor(spect)
            mean = spect.mean(dim=0, keepdim=True)
            # std = spect.std(dim=0, keepdim=True)
            mean = torch.FloatTensor(scipy.ndimage.filters.gaussian_filter1d(mean.numpy(), 20))
            max_mean = mean.mean()
            # std = torch.FloatTensor(scipy.ndimage.filters.gaussian_filter1d(std.numpy(), 20))
            spect.add_(-max_mean)
            # print(spect.min(), spect.max(), spect.mean())
            # spect.div_(std + 1e-8)
        elif not self.normalize or self.normalize == 'none':
            spect = np.log1p(spect)
            spect = torch.FloatTensor(spect)
        else:
            raise Exception("No such normalization")
        return spect

    def parse_transcript(self, transcript_path):
        raise NotImplementedError


TS_CACHE = {}


class SpectrogramDataset(Dataset, SpectrogramParser):
    def __init__(self, audio_conf, manifest_filepath, cache_path, labels, normalize=False, augment=False,
                 max_items=None, curriculum_filepath=None):
        """
        Dataset that loads tensors via a csv containing file paths to audio files and transcripts separated by
        a comma. Each new line is a different sample. Example below:

        /path/to/audio.wav,/path/to/audio.txt,3.5

        Curriculum file format (if used):
        wav,transcript,reference,offsets,cer,wer
        ...

        :param audio_conf: Dictionary containing the sample rate, window and the window length/stride in seconds
        :param manifest_filepath: Path to manifest csv as describe above
        :param labels: String containing all the possible characters to map to
        :param normalize: Apply standard mean and deviation normalization to audio tensor
        :param augment(default False):  Apply random tempo and gain perturbations
        :param curriculum_filepath: Path to curriculum csv as describe above
        """
        with open(manifest_filepath, newline='') as f:
            reader = csv.reader(f)
            ids = [(row[0], row[1], row[2] if len(row) > 2 else 0) for row in reader]
        if max_items:
            ids = ids[:max_items]
        # print("Found entries:", len(ids))
        # self.all_ids = ids
        self.curriculum = None
        self.all_ids = ids
        self.ids = ids
        self.size = len(self.ids)
        self.labels = Labels(labels)
        if curriculum_filepath:
            with open(curriculum_filepath, newline='') as f:
                reader = csv.DictReader(f)
                rows = [row for row in reader]
                for r in rows:
                    r['cer'] = float(r['cer'])
                    r['wer'] = float(r['wer'])
                self.curriculum = {row['wav']: row for row in rows}
        else:
            self.curriculum = {wav: {'wav': wav,
                                     'text': '',
                                     'transcript': '',
                                     'offsets': None,
                                     'cer': 0.999,
                                     'wer': 0.999} for wav, txt, dur in tq(ids, desc='Loading')}
        super(SpectrogramDataset, self).__init__(audio_conf, cache_path, normalize, augment)

    def __getitem__(self, index):
        sample = self.ids[index]
        audio_path, transcript_path, dur = sample[0], sample[1], sample[2]
        spect = self.parse_audio(audio_path)
        reference = self.parse_transcript(transcript_path)
        return spect, reference, audio_path

    def get_curriculum_info(self, item):
        audio_path, transcript_path, _dur = item
        if audio_path not in self.curriculum:
            return self.get_reference_transcript(transcript_path), 0.999
        return self.curriculum[audio_path]['text'], self.curriculum[audio_path]['cer']

    def set_curriculum_epoch(self, epoch, sample=False):
        if sample:
            self.ids = list(
                Curriculum.sample(self.all_ids, self.get_curriculum_info, epoch=epoch, min=len(self.all_ids) / 2))
        else:
            self.ids = self.all_ids.copy()
        np.random.seed(epoch)
        np.random.shuffle(self.ids)
        self.size = len(self.ids)

    def update_curriculum(self, audio_path, reference, transcript, offsets, cer, wer):
        self.curriculum[audio_path] = {
            'wav': audio_path,
            'text': reference,
            'transcript': transcript,
            'offsets': offsets,
            'cer': cer,
            'wer': wer
        }

    def save_curriculum(self, fn):
        with open(fn, 'w') as f:
            writer = csv.DictWriter(f, ['wav', 'text', 'transcript', 'offsets', 'cer', 'wer'])
            writer.writeheader()
            for cl in self.curriculum.values():
                writer.writerow(cl)

    def parse_transcript(self, transcript_path):
        global TS_CACHE
        if transcript_path not in TS_CACHE:
            if not transcript_path:
                ts = self.labels.parse('')
            else:
                with open(transcript_path, 'r', encoding='utf8') as transcript_file:
                    ts = self.labels.parse(transcript_file.read())
            TS_CACHE[transcript_path] = ts
        return TS_CACHE[transcript_path]

    def __len__(self):
        return self.size

    def get_reference_transcript(self, txt):
        return self.labels.render_transcript(self.parse_transcript(txt))


def _collate_fn(batch):
    def func(p):
        return p[0].size(1)

    batch = sorted(batch, key=lambda sample: sample[0].size(1), reverse=True)
    longest_sample = max(batch, key=func)[0]
    freq_size = longest_sample.size(0)
    minibatch_size = len(batch)
    max_seqlength = longest_sample.size(1)
    inputs = torch.zeros(minibatch_size, 1, freq_size, max_seqlength)
    input_percentages = torch.FloatTensor(minibatch_size)
    target_sizes = torch.IntTensor(minibatch_size)
    targets = []
    filenames = []
    for x in range(minibatch_size):
        sample = batch[x]
        tensor = sample[0]
        target = sample[1]
        filenames.append(sample[2])
        seq_length = tensor.size(1)
        inputs[x][0].narrow(1, 0, seq_length).copy_(tensor)
        input_percentages[x] = seq_length / float(max_seqlength)
        target_sizes[x] = len(target)
        targets.extend(target)
    targets = torch.IntTensor(targets)
    return inputs, targets, filenames, input_percentages, target_sizes


class AudioDataLoader(DataLoader):
    def __init__(self, *args, **kwargs):
        """
        Creates a data loader for AudioDatasets.
        """
        super(AudioDataLoader, self).__init__(*args, **kwargs)
        self.collate_fn = _collate_fn


class BucketingSampler(Sampler):
    def __init__(self, data_source, batch_size=1):
        """
        Samples batches assuming they are in order of size to batch similarly sized samples together.
        """
        super(BucketingSampler, self).__init__(data_source)
        self.data_source = data_source
        ids = list(range(0, len(data_source)))
        self.bins = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]

    def __iter__(self):
        for ids in self.bins:
            np.random.shuffle(ids)
            yield ids

    def __len__(self):
        return len(self.bins)

    def shuffle(self, epoch):
        np.random.shuffle(self.bins)


class DistributedBucketingSampler(Sampler):
    def __init__(self, data_source, batch_size=1, num_replicas=None, rank=None):
        """
        Samples batches assuming they are in order of size to batch similarly sized samples together.
        """
        super(DistributedBucketingSampler, self).__init__(data_source)
        if num_replicas is None:
            num_replicas = get_world_size()
        if rank is None:
            rank = get_rank()
        self.data_source = data_source
        self.ids = list(range(0, len(data_source)))
        self.batch_size = batch_size
        self.bins = [self.ids[i:i + batch_size] for i in range(0, len(self.ids), batch_size)]
        self.num_replicas = num_replicas
        self.rank = rank
        self.num_samples = int(math.ceil(len(self.bins) * 1.0 / self.num_replicas))
        self.total_size = self.num_samples * self.num_replicas

    def __iter__(self):
        offset = self.rank
        # add extra samples to make it evenly divisible
        bins = self.bins + self.bins[:(self.total_size - len(self.bins))]
        assert len(bins) == self.total_size
        samples = bins[offset::self.num_replicas]  # Get every Nth bin, starting from rank
        return iter(samples)

    def __len__(self):
        return self.num_samples

    def shuffle(self, epoch):
        # deterministically shuffle based on epoch
        g = torch.Generator()
        g.manual_seed(epoch)
        bin_ids = list(torch.randperm(len(self.bins), generator=g))
        self.bins = [self.bins[i] for i in bin_ids]


def get_audio_length(path):
    output = subprocess.check_output(['soxi -D \"%s\"' % path.strip().replace('"', '\\"')], shell=True)
    return float(output)


def audio_with_sox(path, sample_rate, start_time, end_time):
    """
    crop and resample the recording with sox and loads it.
    """
    with NamedTemporaryFile(suffix=".wav") as tar_file:
        tar_filename = tar_file.name
        sox_params = "sox \"{}\" -r {} -c 1 -b 16 -e si {} trim {} ={} >>sox.1.log 2>>sox.2.log".format(
            path.replace('"', '\\"'), sample_rate,
            tar_filename, start_time, end_time)
        os.system(sox_params)
        y, sample_rate_ = load_audio(tar_filename)
        assert sample_rate == sample_rate_
        return y, sample_rate


def augment_audio_with_sox(path, sample_rate, tempo, gain, channel=-1):  # channels: -1 = both, 0 = left, 1 = right
    """
    Changes tempo and gain of the recording with sox and loads it.
    """
    with NamedTemporaryFile(suffix=".wav") as augmented_file:
        augmented_filename = augmented_file.name
        sox_augment_params = ["tempo", "{:.3f}".format(tempo), "gain", "{:.3f}".format(gain)]
        if channel != -1:
            sox_augment_params.extend(["remix", str(channel + 1)])
        sox_params = "sox \"{}\" -r {} -c 1 -b 16 -t wav -e si {} {} >>sox.1.log 2>>sox.2.log".format(
            path.replace('"', '\\"'),
            sample_rate,
            augmented_filename,
            " ".join(sox_augment_params))
        os.system(sox_params)
        y, sample_rate_ = load_audio(augmented_filename)
        assert sample_rate == sample_rate_
        return y, sample_rate


def load_randomly_augmented_audio(path, sample_rate=16000, tempo_range=(0.85, 1.15),
                                  gain_range=(-10, 10), channel=-1):
    """
    Picks tempo and gain uniformly, applies it to the utterance by using sox utility.
    Returns the augmented utterance.
    """
    low_tempo, high_tempo = tempo_range
    tempo_value = np.random.uniform(low=low_tempo, high=high_tempo)
    low_gain, high_gain = gain_range
    gain_value = np.random.uniform(low=low_gain, high=high_gain)
    audio, sample_rate_ = augment_audio_with_sox(path=path, sample_rate=sample_rate,
                                                 tempo=tempo_value, gain=gain_value, channel=channel)
    assert sample_rate == sample_rate_
    return audio, sample_rate
