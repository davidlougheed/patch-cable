#!/usr/bin/env python3

import pyaudio
import math
import numpy as np
import time
import itertools
import random
import functools
import threading


SAMPLE_RATE = 19200.0
VOLUME = 0.5
FRAME_SIZE = 1024

BEAT_32ND = SAMPLE_RATE / 32.0
BEAT_16TH = SAMPLE_RATE / 16.0
BEAT_8TH = SAMPLE_RATE / 8.0
BEAT_4TH = SAMPLE_RATE / 4.0
BEAT_HALF = SAMPLE_RATE / 2.0
BEAT_WHOLE = SAMPLE_RATE

TWO_PI = 2.0 * math.pi

p = pyaudio.PyAudio()

global_watchers = []

inputs = []


class Node:
    def id(self, x):
        return x

    def __init__(self):
        self.upstream = []
        self.upstream_count = 0  # Caching
        self.downstream = []
        self.function = self.id
        self.value = self.function(0)

    def register_upstream(self, up):
        self.upstream.append(up)
        self.upstream_count = len(self.upstream)
        up.attach_downstream(self)
        return self

    def attach_downstream(self, ds):
        self.downstream.append(ds)

    def register_downstream(self, ds):
        self.attach_downstream(ds)
        ds.register_upstream(self)
        return self

    def step(self):
        self.value = self.function(sum([up.value for up in self.upstream]) / self.upstream_count)

    def run_chain(self):
        self.step()
        for ds in self.downstream:
            ds.run_chain()

    def reset_chain(self):
        self.value = self.function(0)
        for ds in self.downstream:
            ds.reset_chain()


class SourceNode(Node):
    def __init__(self, use_global_steps=False):
        global global_steppers

        super().__init__()
        self._x = 0
        self.use_global_steps = use_global_steps

        if self.use_global_steps:
            global_steppers.append(self)

    def step(self):
        self._x += 1
        self.value = self.function(self._x)

    def reset_chain(self):
        self._x = 0
        super().reset_chain()


class ChainStartNode(SourceNode):
    def __init__(self, start_param, gate=0.01):
        global global_watchers

        super().__init__()
        self.start_param = start_param
        self.gate = gate
        self.started = False
        self.chain = None

        global_watchers.append(self)

    def tick(self):
        if self.chain is not None:
            if self.start_param.value >= self.gate and not self.chain.started:
                self.chain.play_chain()
            elif self.start_param.value < self.gate and self.chain.started and not self.chain.terminating:
                self.chain.stop_chain()

    def reset_chain(self):
        self.started = False
        super().reset_chain()


class ChainTerminationNode(Node):
    def __init__(self, release_chain=None):
        super().__init__()
        self.release_chain = release_chain


class RandomNoiseNode(SourceNode):
    def noise_fn(self, _):
        return self.translate + random.random() * self.amplitude

    def __init__(self, use_global_steps=False, translate=0.0, amplitude=1.0):
        super().__init__(use_global_steps)
        self.translate = translate
        self.amplitude = amplitude
        self.function = self.noise_fn


class SineNode(SourceNode):
    def sin(self, x):
        return self.translate + self.amplitude * math.sin(TWO_PI * (x / SAMPLE_RATE) * self.frequency)

    def __init__(self, frequency=440.0, use_global_steps=False, translate=0.0, amplitude=1.0):
        super().__init__(use_global_steps)
        self.frequency = frequency
        self.function = self.sin
        self.translate = translate
        self.amplitude = amplitude


class SquareNode(SourceNode):
    def square(self, x):
        return math.copysign(1, math.sin(TWO_PI * (x / SAMPLE_RATE) * self.frequency))

    def __init__(self, frequency=440.0, use_global_steps=False):
        super().__init__(use_global_steps)
        self.frequency = frequency
        self.function = self.square


class SawtoothNode(SourceNode):
    def sawtooth(self, x):
        return self.amplitude * (-2.0 / math.pi * math.atan(1.0/math.tan(x * math.pi / (SAMPLE_RATE / self.frequency))))

    def __init__(self, frequency=440.0, use_global_steps=False, amplitude=0.5):
        super().__init__(use_global_steps)
        self.frequency = frequency
        self.function = self.sawtooth
        self.amplitude = amplitude


class BeatNode(SourceNode):
    def beat_fn(self, x):
        return self.translate + (self.amplitude if x % self.period_length <= self.beat_length else 0)

    def __init__(
            self,
            use_global_steps=False,

            translate=0.0,
            amplitude=1.0,

            beat_length=BEAT_4TH,
            gap_length=BEAT_4TH
    ):
        super().__init__(use_global_steps)
        self.translate = translate
        self.amplitude = amplitude
        self.beat_length = beat_length
        self.gap_length = gap_length
        self.period_length = self.beat_length + self.gap_length
        self.function = self.beat_fn


class FilterNode(Node):
    def filter_fn(self, x):
        return x * self.filter_param.value

    def __init__(self, filter_param):
        super().__init__()
        self.filter_param = filter_param

        self.function = self.filter_fn


class LinearDecayNode(Node):
    def decay_fn(self, y):
        return (max(self.duration - self._x, 0) / self.duration) * y

    def __init__(self, duration=BEAT_HALF):
        super().__init__()
        self.duration = duration
        self.function = self.decay_fn
        self._x = 0

    def step(self):
        self._x += 1
        super().step()


cached_repeat = range(FRAME_SIZE)


class Chain:
    def __init__(self, source_node, termination_node, duration=-1.0):
        global global_watchers

        global_watchers.append(self)

        self.source_node = source_node
        self.termination_node = termination_node

        self.source_node.chain = self
        self.termination_node.chain = self

        self.stream = None
        self.started = False
        self.terminating = False

        self.time_elapsed = 0.0
        self.duration = duration

    def play_chain(self):
        if self.started:
            return

        sn = self.source_node

        def run_chain(nodes):
            while len(nodes) > 0:
                new_nodes = []
                for n in nodes:
                    n.step()
                    new_nodes.extend(n.downstream)
                nodes = list(set(new_nodes))

        def callback(_in_data, _frame_count, _time_info, _status):
            output_samples = []

            for _ in cached_repeat:
                run_chain([sn])
                output_samples.append(self.termination_node.value)

            return np.array(output_samples, dtype=np.float32) * VOLUME, pyaudio.paContinue

        self.stream = p.open(format=pyaudio.paFloat32, channels=1, rate=int(SAMPLE_RATE), output=True,
                             frames_per_buffer=FRAME_SIZE, stream_callback=callback)

        self.started = True

    def stop_chain(self):
        if self.started and self.duration >= 0:
            if self.time_elapsed < self.duration:
                return

        if self.termination_node.release_chain is not None and self.termination_node.release_chain.duration >= 0.0:
            self.terminating = True

            self.termination_node.release_chain.source_node.register_upstream(self.termination_node)
            duration = self.termination_node.release_chain.duration
            self.termination_node = self.termination_node.release_chain.termination_node

            self.duration = (self.duration if self.duration > 0 else 0) + duration

            return

        self.stream.stop_stream()
        self.stream.close()
        self.stream = None

        self.started = False

        self.source_node.reset_chain()

    def tick(self):
        if self.started and self.duration >= 0:
            self.time_elapsed += 1.0/64.0 * SAMPLE_RATE
            if self.time_elapsed > self.duration:
                self.stop_chain()

    def set_duration(self, duration):
        self.duration = duration
        return self

    @property
    def value(self):
        return self.termination_node.value

    @staticmethod
    def build_linear(*nodes):
        old_node = None

        for n in nodes:
            if old_node is not None:
                n.register_upstream(old_node)
            old_node = n

        return Chain(nodes[0], nodes[-1])


class Parameter:
    PARAM_CONSTANT = 'PARAM_CONSTANT'
    PARAM_CHAIN = 'PARAM_CHAIN'
    PARAM_INPUT = 'PARAM_INPUT'

    def __init__(self, param_value):
        if isinstance(param_value, Chain):
            self.param_type = Parameter.PARAM_CHAIN
        elif isinstance(param_value, float):
            self.param_type = Parameter.PARAM_CONSTANT
        else:  # TODO
            self.param_type = Parameter.PARAM_INPUT

        self.param_value = param_value

    @property
    def value(self):
        global inputs
        if self.param_type == Parameter.PARAM_CONSTANT:
            return self.param_value
        if self.param_type == Parameter.PARAM_CHAIN:
            return self.param_value.value
        if self.param_type == Parameter.PARAM_INPUT:
            return inputs[self.param_value - 1]
        else:
            return 0


test_decay = Chain.build_linear(
    LinearDecayNode(duration=BEAT_WHOLE * 2),
    ChainTerminationNode()
).set_duration(BEAT_WHOLE * 2)

# test_filter_param = Parameter(Chain.build_linear(
#     BeatNode(use_global_steps=True, beat_length=BEAT_4TH, gap_length=BEAT_4TH*3),
#     ChainTerminationNode()
# ))


something_param = Parameter(0.0)


# test_dummy = ChainStartNode(something_param)
# test_source = RandomNoiseNode(amplitude=1)\
#     .register_upstream(test_dummy)
# test_source_2 = SquareNode(220.0)\
#     .register_upstream(test_dummy)
# test_filter = FilterNode(test_filter_param)\
#     .register_upstream(test_source)
# test_out = ChainTerminationNode(release_chain=test_decay) \
#     .register_upstream(test_filter) \
#     .register_upstream(test_source_2)
#
# test_chain = Chain(test_dummy, test_out)


test_dummy = ChainStartNode(something_param)  # SourceNode()

test_source_1 = SineNode(frequency=164.81).register_upstream(test_dummy)
test_source_2 = SineNode(frequency=196.00).register_upstream(test_dummy)
# test_source_3 = SineNode(frequency=246.94).register_upstream(test_dummy)
# test_source_4 = SineNode(frequency=329.63).register_upstream(test_dummy)
# test_source_5 = SineNode(frequency=392.00).register_upstream(test_dummy)
# test_source_6 = SineNode(frequency=493.88).register_upstream(test_dummy)

test_out = ChainTerminationNode(release_chain=test_decay)\
    .register_upstream(test_source_1) \
    .register_upstream(test_source_2) # \
    # .register_upstream(test_source_3) #\
    # .register_upstream(test_source_4)\
    # .register_upstream(test_source_5)

"""
\
    .register_upstream(test_source_2)\
    .register_upstream(test_source_3)\
    .register_upstream(test_source_4)\
    .register_upstream(test_source_5)\
    .register_upstream(test_source_6)"""

test_chain = Chain(test_dummy, test_out)

# test_chain = Chain.build_linear(test_dummy, test_out)


def event_handler():
    global global_watchers
    while True:
        for w in global_watchers:
            w.tick()
        time.sleep(1.0/64.0)


t = threading.Thread(target=event_handler)
t.start()

variables = {}

time.sleep(1)

something_param.param_value = 1.0

time.sleep(4)

something_param.param_value = 0.0

# test_chain.play_chain()

# p.terminate()

# TODO: Quantizing?
