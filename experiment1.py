import os
import datetime
import dill
import time
import sys
import winsound
import myo
import numpy
import pathlib
import pyqtgraph as pg

from argparse import ArgumentParser
from configparser import ConfigParser

from axopy import util
from axopy.gui.canvas import Canvas, Text, Circle
from axopy.daq import _Sleeper
from axopy.experiment import Experiment
from axopy.features import MeanAbsoluteValue

from axopy.gui.graph import SignalWidget, _MultiPen
from axopy.gui.main import qt_key_map
from axopy.pipeline import Pipeline, Windower, \
                           Ensure2D, FeatureExtractor
from axopy.task import Task
sys.path.append(os.path.join(os.getcwd(), 'streamhist'))
from histogram import StreamHist
from axopy.timing import StepCounter, Counter

from pydaqs.myo import MyoEMG

from PyQt5 import QtWidgets, QtCore #, Qt
# from PyQt5.QtGui import QIcon

# from emg32 import emg32DAQ
import graphics as gr

# conda install sortedcontainers
sys.path.append(os.path.join(os.getcwd(), 'streamhist'))
from histogram import StreamHist

import simplepyble
import threading
import queue

'''
TODO:
    This needs an BLE connection which can gracefully fail.
    System works fine without BLE.
    With BLE sends signals on target transition.
'''

class FeedbackBLE():
    """BLE Connection to ESP32S3 to send control commands.
    """
    def __init__(self):
        self.device_name = "Feedback BLE"
        self.service_uuid = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
        self.characteristic_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
        self.peripheral = None
        self.connected = False
        self.q = queue.Queue()

    def ble_thread(self):
        """Send BLE commands in a seperate thread to ensure no blocking."""
        while True:
            item = self.q.get()
            self.peripheral.write_request(self.service_uuid, 
                                          self.characteristic_uuid,
                                          str.encode(item))
            self.q.task_done()

    def start(self):
        # get ble adapter
        adapters = simplepyble.Adapter.get_adapters()
        if len(adapters) == 0:
            print("No bluetooth adapter found")
        if len(adapters) > 1:
            print("Multiple Bluetooth adapters found")
        adapter = adapters[0]

        # scan
        adapter.scan_for(1000)
        peripherals = adapter.scan_get_results()  
        for i, p in enumerate(peripherals):
            if p.identifier() == self.device_name:
                self.peripheral = peripherals[i] 
        if self.peripheral is None:
            for i in range(3):
                print("Cannot find bluetooth device")
                time.sleep(0.5)       
        else:
            self.peripheral.connect()
            self.connected = True
            self.t = threading.Thread(target=self.ble_thread, daemon=True)
            self.t.start()

    def stop(self):
        if self.connected:
            self.peripheral.disconnect()
        self.connected = False
        self.q.join()

    def send(self, cmd):
        if self.connected:
            content = str(cmd)
            self.q.put(content)

class FakeMyo(QtCore.QObject):
    """Fake Myo

        This is a fake Myo input that can be used for development.
        Use buttons 1 to 8 to create fake EMG signals.

        For documentation see these examples:
            axopy/devices.py emgsim
            axopy/daq.py keyboard   
    """

    def __init__(self, rate=200, read_length=0.05, keys=None):
        super(FakeMyo, self).__init__()
        self.rate = rate
        if keys is None:
            keys = list('12345678')
        self.keys = keys
        self.gain = 0.5
        self.chunk_size = int(rate*read_length)
        self._qkeys = [qt_key_map[k] for k in keys]
        self._sleeper = _Sleeper(read_length)
        self._data = numpy.zeros((len(self.keys), 1))
        self.pipeline = Pipeline([Windower(self.rate)])


    def start(self):
        # install event filter to capture keyboard input events
        get_qtapp().installEventFilter(self)

    def read(self):
        self._sleeper.sleep()
        keys = self._data.copy()
        win_keys = self.pipeline(keys)
        ave_keys = numpy.expand_dims(numpy.mean(win_keys, axis=1), axis=1)
        out = self.gain * ave_keys * numpy.random.randn(ave_keys.shape[0], ave_keys.shape[1])
        if self.chunk_size > 1:
            out = numpy.repeat(out, self.chunk_size, axis=1)
        self._data *= 0
        return out

    def stop(self):
        get_qtapp().removeEventFilter(self)

    def reset(self):
        self._sleeper.reset()

    def eventFilter(self, obj, event):
        evtype = event.type()
        if evtype == QtCore.QEvent.KeyPress and event.key() in self._qkeys:
            self._data[self._qkeys.index(event.key())] = 1
            return True
        return False
    
    def connect(self):
        pass


def get_qtapp():
    """Get a QApplication instance running.
       Copied from axopy.gui main.py
    """
    global qtapp
    inst = QtWidgets.QApplication.instance()
    if inst is None:
        qtapp = QtWidgets.QApplication(sys.argv)
    else:
        qtapp = inst
    return qtapp


class DebugPrinter(object):
    def __init__(self):
        self.last_read_time = None
        self.start_time = None

    def print(self, stage):
        t = time.time()
        if self.last_read_time is None:
            pass
        else:
            now = (t - self.start_time)
            ms = (t - self.last_read_time)
            pstr = 'time: {:.4f} inc: {:.4f} @ {}'.format(now, ms, stage)
            print(pstr)
        self.last_read_time = t

    def start(self):
        t = time.time()
        self.last_read_time = t
        self.start_time = t

    def reset(self):
        self.last_read_time = None
        self.start_time = None


class MySignalWidget(SignalWidget):
    """ Overwrite SignalWidget.
    Add useful scaling tools.
    Add graphics to indicate what we are doing.
    Add plotting data in multiple columns
    Add highlighting individual plot.
    Add normalisation bar for each plot.

    Fix y-scaling on autoscale
    """
    def __init__(self, channel_names=None, bg_color=None, yrange=(-1,1),
                 show_bottom=False, xlabel=None, columns=1):
        super(MySignalWidget, self).__init__(channel_names, bg_color, yrange, show_bottom, xlabel)

        self.autoscale = True
        self.plot_columns = columns
        self.plot_select_row = 0
        self.plot_select_col = 0
        self.ch_highlight = -1

        #
        # shink bar graph width
        #
        qGraphicsGridLayout = self.ci.layout
        for i in range(self.plot_columns * 2):
            if i % 2 == 0:
                qGraphicsGridLayout.setColumnStretchFactor(i, 5)
            else:
                qGraphicsGridLayout.setColumnStretchFactor(i, 1)
                

    def plot_highlight(self, row, col):
        # update row and column
        self.plot_select_row += row
        self.plot_select_col += col

        self.plot_select_row = min(max(self.plot_select_row, 0), self.plot_rows - 1)
        self.plot_select_col = min(max(self.plot_select_col, 0), (self.plot_columns * 2) - 2)

        key = str(self.plot_select_row) + "-" + str(self.plot_select_col)
        self.ch_highlight = self.plot_rc[key]
        
        #print("%i, %i, %i" % ( self.plot_select_row, self.plot_select_col, self.ch_highlight))

        for i in range(self.n_channels):
            if i == self.ch_highlight:
                self.plot_data_items[i].setPen(pg.mkColor('k'))
            else:
                self.plot_data_items[i].setPen(pg.mkColor('b'))

    def plot_scope(self, y, x=None):
        super(MySignalWidget, self).plot(y, x)
        for i, pdi in enumerate(self.plot_items):
            pdi.setYLink(None)
            if self.autoscale:
                pdi.enableAutoRange(pg.ViewBox.YAxis)

                pdi.disableAutoRange(pg.ViewBox.YAxis)
                # fix to ensure equal y range
                _temp = pdi.getViewBox().state
                _yrange = _temp['viewRange'][1]
                _yR = max(abs(_yrange[0]), abs(_yrange[1]))
                pdi.setYRange(-_yR, +_yR)
            else:
                pdi.setYRange(self.yrange[0], self.yrange[1])

    def plot_bar(self, y):
        for i, pdi in enumerate(self.plot_bar_items):
            pdi.setOpts(height=y[i])

    def _update_num_channels(self):
        self.clear()
        self.plot_items = []
        self.plot_data_items = []
        self.plot_bar_items = []

        # not sure when self.n_channels is set
        self.plot_rows = int(self.n_channels / self.plot_columns)
        self.plot_rc = {}

        pen = _MultiPen(self.n_channels)

        plot_row = 0
        plot_col = 0
        for i, name in zip(range(self.n_channels), self.channel_names):
            
            self.plot_rc[str(plot_row) + "-" + str(plot_col)] = i

            plot_item = self.addPlot(row=plot_row, col=plot_col)
            plot_data_item = plot_item.plot(pen=pg.mkColor('b'), antialias=True, clickable=True)

            if self.show_bottom is not True:
                plot_item.showAxis('bottom', False)
            plot_item.showGrid(y=True, alpha=0.5)
            plot_item.setMouseEnabled(x=False)
            plot_item.setMenuEnabled(False)

            if self.n_channels > 1:
                label = "{}".format(name)
                plot_item.setLabels(left=label)

            if i > 0:
                plot_item.setYLink(self.plot_items[0])

            self.plot_items.append(plot_item)
            self.plot_data_items.append(plot_data_item)

            # add a column for the bar          
            plot_col+=1
            plot_bar_item = pg.BarGraphItem(x=[0.5], height=[0.15], width=1, brush='b', pen='b')

            plot_item = self.addPlot(row=plot_row, col=plot_col)
            plot_item.addItem(plot_bar_item)
            plot_item.setYRange(0, 1.1)
            plot_item.showAxis('bottom', False)

            self.plot_bar_items.append(plot_bar_item)

            plot_col+=1
            # take into account bars
            if plot_col >= self.plot_columns * 2:
                plot_row+=1
                plot_col=0

        self.plot_items[0].disableAutoRange(pg.ViewBox.YAxis)
        self.plot_items[0].setYRange(*self.yrange)
        if self.show_bottom == 'last':
            self.plot_items[-1].showAxis('bottom', True)

        if self.xlabel is not None:
            self.plot_items[-1].setLabels(bottom=self.xlabel)

    def increaseYRange(self):
        _new_y = numpy.minimum(self.yrange[1] + NORMALISE_Y_RANGE_INCREMENT, NORMALISE_Y_RANGE_MAX)
        self.yrange = (-_new_y, _new_y)

    def decreaseYRange(self):
        _new_y = numpy.maximum(self.yrange[1] - NORMALISE_Y_RANGE_INCREMENT, NORMALISE_Y_RANGE_MIN)
        self.yrange = (-_new_y, _new_y)
        
    def setY(self, value):
        _new_y = numpy.clip(value, NORMALISE_Y_RANGE_MIN, NORMALISE_Y_RANGE_MAX)
        self.yrange = (-_new_y, _new_y)

    def getY(self):
        return self.yrange[1]

    def toggleAutoscale(self):
        self.autoscale = not self.autoscale


class MyExperiment(Experiment):
    """ Overwrite experiment class.
    Removes the need to press Enter to start experiment.
    """
    def __init__(self, daq=None, data='data', subject=None,
                 allow_overwrite=False):
        super(MyExperiment, self).__init__(daq, data, subject, allow_overwrite)
        
        self.autostart = True

        if FULLSCREEN:
            app = get_qtapp()
            self.screen.windowHandle().setScreen(app.screens()[MONITOR])
            self.screen.showFullScreen()
        elif MAXSCREEN:
            monitor = QtWidgets.QDesktopWidget().screenGeometry(MONITOR)
            self.screen.move(monitor.left(), monitor.top())
            self.screen.resize(monitor.width(), monitor.height())
            self.screen.showMaximized()
        elif LEFTSCREEN:
            monitor = QtWidgets.QDesktopWidget().screenGeometry(MONITOR)
            self.screen.move(monitor.left(), monitor.top())
            self.screen.resize(int(monitor.width() * 0.5), int(monitor.height() * 0.95))
        elif RIGHTSCREEN:
            monitor = QtWidgets.QDesktopWidget().screenGeometry(MONITOR)
            self.screen.move(monitor.width() * 0.5, monitor.top())
            self.screen.resize(int(monitor.width() * 0.5), int(monitor.height() * 0.95))

    def _task_finished(self):
        super(MyExperiment, self)._task_finished()
        # first call starts _run_task (pressing enter #1)
        if self.autostart:
            self._run_task()
            self.autostart = False


class MyTask(Task):
    """ Overwrite task class.
        1. Removes the need to press Enter to start task.
        2. Includes filtering, normalisation, save and load. 
    """
    def __init__(self):
        super(MyTask, self).__init__()

        ''' 
        Quantiles can be extracted for normalisation
        '''
        # self.hist_norm_quantiles = (0.05, 0.5, 0.95)
        # self.hist_quants = [None] * N_CHANNELS

        self.hist_array = [None] * N_CHANNELS
        self.hist_stats = [None] * N_CHANNELS
        self.reset_calib_array()

    def run(self): 
        super(MyTask, self).run()
        # run calls next_trial (pressing enter #2
        self.next_trial()
        self.run_scope()

    def next_trial(self):
        # override axopy method to avoid run_trial
        # and any finishing of blocks
        trial = self.iter.next_trial()
        if trial is None:
            self.finish_block()
            return
        self.trial = trial

    def run_scope(self):
        """ Run umbrella scope task.
        """
        pass

    def make_pipeline_mav(self):
        pipeline = Pipeline([        
            Windower(int(S_RATE * MAV_WIN_SIZE)),
            FeatureExtractor([('MAV', MeanAbsoluteValue())], N_CHANNELS),
            Ensure2D(orientation='col')
        ])
        return pipeline
    
    def apply_normalisation(self, data):
        norm = numpy.zeros(data.shape)
        for _ch in range(N_CHANNELS):
            if not numpy.isnan(self.calib_array[_ch, 0]):
                _min = self.calib_array[_ch, 0]
                _max = self.calib_array[_ch, 1]             
                norm[_ch, :] = data[_ch, :] - _min
                norm[_ch, :] = norm[_ch, :] / (_max - _min)
                norm[_ch, norm[_ch, :] < 0] = 0
        return norm

    def reset_calib_array(self):
        self.calib_array = numpy.ones((N_CHANNELS, 2)) * numpy.nan

    def populate_calib_array(self):
        for _ch in range(N_CHANNELS):
            if self.hist_stats[_ch]:
                self.calib_array[_ch, 0] = self.hist_stats[_ch][0]
                self.calib_array[_ch, 1] = self.hist_stats[_ch][1]

    def save_calibration(self):
        _timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d-%H-%M-%S')
        _file = 'calib_' + _timestamp
        _path = os.path.join(self.writer.root, _file)
        
        # numpy.save(_path, self.calib_array)
        # print(self.calib_array)
        dill.dump([self.calib_array, self.hist_array], file = open(_path + ".dill", "wb"))
        print(_path)
        print(self.calib_array)

    def load_calibration(self):
        # list of potential folders in reverse time
        _path = os.path.dirname(self.writer.root)     
        _walk = [x[0] for x in os.walk(_path)]
        _walk.reverse()
        _loadpath = None
        
        # find first calib file 
        for _w in _walk:
            for _f in os.listdir(_w):
                if _f.startswith('calib'):
                    if not _loadpath:
                        _loadpath = os.path.join(_w, _f)
                        print(_loadpath)                

        if _loadpath:
            #_data = numpy.load(_loadpath)
            #self.calib_array = _data
            [self.calib_array, self.hist_array] = dill.load(open(_loadpath, "rb"))
            print(self.calib_array)


class RecordingScopeTask(MyTask):
    """ Base acquisition task.
    Can be used to view data synched in a single scope.
    """
    def __init__(self, folder_prefix='calib'):
        super(RecordingScopeTask, self).__init__()
        self.debug_printer = DebugPrinter()
        self.folder_prefix = folder_prefix       
        self.display_update_counter = 0
        self.record_update_counter = 0
        self.record_update_on = False
        self.trial_running = False
        self.ch_hist_running = False
        self.all_hist_running = False

        self.pipeline_display = self.make_pipeline_display()     
        self.pipeline_mav = self.make_pipeline_mav()

    def make_pipeline_display(self):
        pipeline = Pipeline([        
            Windower(int(S_RATE * DISPLAY_WIN_SIZE)),
        ])
        return pipeline
        
    def prepare_design(self, design):
        block = design.add_block()
        for t in range(MAX_N_TRIALS):
            block.add_trial()

    def prepare_storage(self, storage):
        timestamp = time.strftime('%Y%m%d%H%M%S',
                                  time.localtime())
        self.writer = storage.create_task(self.folder_prefix + '_' + timestamp)

    def get_step(self, time_value):
        return int(time_value / READ_LENGTH)

    def prepare_daq(self, daqstream):
        total_steps = self.get_step(MAX_TRIAL_LENGTH)

        # trial counter is max trial length
        self.counter0 = StepCounter(total_steps)
        self.counter0.timeout.connect(self.finish_trial)
      
        self.daqstream = daqstream
        self.daqstream.start()

    def run_trial(self, trial):
        # sanity check
        if self.trial_running:
            self.debug_printer.print('trial already running!')
            return

        # can we create buffer here?
        _dims = (N_CHANNELS, int(S_RATE * MAX_TRIAL_LENGTH * 60))
        trial.add_bufferedarray('data', insert_axis=1, buffer_dims=_dims)
        trial.add_bufferedarray('proc', insert_axis=1, buffer_dims=_dims)
        trial.add_array('time', stack_axis=1)

        # time and debugging
        self.t_start = time.time()
        self.counter0.reset()
        self.trial_running = True

        self.debug_printer.print('run trial')
        self.debug_printer.print(self.trial)
    
    def run_scope(self):
        # get scopes working
        self.debug_printer.start()
        self.daqstream.updated.connect(self.update)
        self.debug_printer.print('run scope.')

    def finish_trial(self):
        # sanity check
        if not self.trial_running:
            self.debug_printer.print('trial not running!')
            return

        # write out data
        self.trial.arrays['data'].set_data()
        self.trial.arrays['proc'].set_data()
        self.writer.write(self.trial)

        # increment to next trial
        self.trial_running = False
        self.next_trial()
        # self.trial_prompt_update()
        self.debug_printer.print('finish trial')

    def next_block(self):
        '''overried next_block from axopy base.py so that we can 
        put a message in to prompt the user what to do'''
        self.debug_printer.print('Next block, press [ENTER] to continue ... ')

        block = self.iter.next_block()
        if block is None:
            self.finish()
            return

        self.block = block

        # wait for confirmation between blocks
        if self.advance_block_key is None:
            self.next_trial()
        else:
            self._awaiting_key = True
            
    def update(self, data):

        '''
        # trial
        if self.trial_running:
            _timestamp = time.time() - self.t_start
            self.trial.arrays['time'].stack(_timestamp)
            self.trial.arrays['data'].insert(data)
            self.trial.arrays['proc'].insert(proc_data)

            self.counter0.increment()
            self.record_update_counter += 1
            if (self.record_update_counter == DISPLAY_RECORD_RATIO):
                if self.record_update_on:
                    self.record_prompt.setStyleSheet('background-color: red')
                else:
                    self.record_prompt.setStyleSheet('background-color: none')
                self.record_update_counter = 0
                self.record_update_on = not self.record_update_on
        else:
           self.record_prompt.setStyleSheet('background-color: none')
        '''

        # processed mav data 
        proc_data = self.pipeline_mav(data)

        # histogram based normalisation
        norm_proc_data = self.apply_normalisation(proc_data)

        # normalisation histogram
        if self.ch_hist_running:
            _ch = self.scope.ch_highlight
            self.hist_array[_ch].update(proc_data[_ch, :])
        elif self.all_hist_running:
            for _ch in range(N_CHANNELS):
                self.hist_array[_ch].update(proc_data[_ch, :])
        
        # scope 
        if self.pipeline_display is not None:
            scope_data = self.pipeline_display.process(data)

        self.display_update_counter += 1
        if (self.display_update_counter == DISPLAY_UPDATE_RATIO):
            self.scope.plot_scope(scope_data)
            self.scope.plot_bar(norm_proc_data)
            self.display_update_counter = 0
    
    def trial_toggle(self):
        if self.trial_running:
            self.debug_printer.print('request trial stop')
            self.finish_trial()  
        else:
            self.debug_printer.print('request trial start')
            self.run_trial(self.trial)

    def ch_normalisation_toggle(self):
        _ch = self.scope.ch_highlight
        if self.ch_hist_running:
            self.debug_printer.print('request hist stop')

            ''' Retain min and max '''
            self.hist_stats[_ch] = [self.hist_array[_ch].min(), \
                                    self.hist_array[_ch].max()]
            # self.hist_quants[_ch] = self.hist_array[_ch].quantiles(*self.hist_norm_quantiles)
            
            self.ch_hist_running = False
            self.populate_calib_array()
            print(self.hist_array[_ch].describe())

        else:
            if _ch > -1:
                self.debug_printer.print('request hist start')
                if not self.hist_array[_ch]:
                    self.hist_array[_ch] = StreamHist(maxbins=32)
                self.ch_hist_running = True
            else:
                self.debug_printer.print('failed hist start')
    
    def ch_normalisation_reset(self):
        _ch = self.scope.ch_highlight
        if self.ch_hist_running:
            self.debug_printer.print('failed hist reset')
        else:
            if _ch > -1:
                self.debug_printer.print('request hist reset')
                self.hist_stats[_ch] = None
                self.calib_array[_ch, :] = numpy.nan

    def all_normalisation_toggle(self):
        if self.all_hist_running:
            for _ch in range(N_CHANNELS):
                self.hist_stats[_ch] = [self.hist_array[_ch].min(), \
                                        self.hist_array[_ch].max()]
                # self.hist_quants[_ch] = self.hist_array[_ch].quantiles(*self.hist_norm_quantiles)
                # print(self.hist_array[_ch].describe())
            self.all_hist_running = False
            self.populate_calib_array()            
            self.debug_printer.print('request hist stop')
        else:
            for _ch in range(N_CHANNELS):
                self.hist_array[_ch] = StreamHist(maxbins=32)
            self.all_hist_running = True
            self.debug_printer.print('request hist start')

    def all_normalisation_reset(self):
        if self.ch_hist_running or self.all_hist_running:
            self.debug_printer.print('failed hist reset')
        else:
            for _ch in range(N_CHANNELS):
                self.hist_quants[_ch] = None
            self.reset_calib_array()                
            self.debug_printer.print('request hist reset')

    def key_press(self, key):
        super(MyTask, self).key_press(key)

        # meta
        if key == util.key_escape:
            self.finish()
        
        # trial    
        if key == util.key_space:
            self.trial_toggle()

        # channel normalisation
        if key == util.key_n:
            self.ch_normalisation_toggle()

        if key == util.key_m:
            self.ch_normalisation_reset()

        # all normalisation 
        if key == util.key_o:
            self.all_normalisation_toggle()

        if key == util.key_p:
            self.all_normalisation_reset()

        # save and load calibration
        if key == util.key_z:
            self.save_calibration()

        if key == util.key_x:
            self.load_calibration()

        # display autoscaleing
        if key == util.key_i:
            self.scope.setY(NORMALISE_Y_RANGE_DEFAULT)
            self.scope.toggleAutoscale()

        if key == util.key_u:
            self.scope.increaseYRange()

        if key == util.key_y:
            self.scope.decreaseYRange()

        # block if hist running
        hist_running = self.ch_hist_running or self.all_hist_running
        if not hist_running:
            # channel seletion and highlight
            if key == util.key_w:
                self.scope.plot_highlight(-1, 0)

            if key == util.key_s:
                self.scope.plot_highlight(1, 0)

            if key == util.key_a:
                self.scope.plot_highlight(0, -2)

            if key == util.key_d:
                self.scope.plot_highlight(0, 2)

    def finish(self):
        self.disconnect(self.daqstream.updated, self.update)
        # self.daqstream.stop()
        self.finished.emit()

    def play_beep(self):
        winsound.PlaySound('sounds\\beep.wav', 1)
        self.debug_printer.print('play beep')

    ''' Graphics related functions from here down.
        Could refactor into another file but doesn't seem necessary atm.
    '''
    def prepare_graphics(self, container):      
        # scope is the standard signal widget
        self.scope = MySignalWidget(channel_names=DISPLAY_CHANNEL_NAMES, columns=DISPLAY_COLUMNS)

        vBoxLayout = QtWidgets.QGridLayout()
        vBoxLayout.addWidget(self.scope, 0, 0, 0, 1)

        vBoxLayout.setRowStretch(0, 2) 
        vBoxLayout.setRowStretch(1, 1)
        vBoxLayout.setRowStretch(2, 1)
        
        container.layout = vBoxLayout   
        container.setLayout(container.layout)
            

class AbstractControl(MyTask):
    """Real time abstract control
    """
    def __init__(self, folder_prefix='control'):
        super(AbstractControl, self).__init__()

        self.ble_feedback = FeedbackBLE()
        self.ble_feedback.start()

        self.debug_printer = DebugPrinter()
        self.debug_printer.start()

        self.folder_prefix = folder_prefix  
        self.ctrl_channels = numpy.array(CTRL_CHANNELS) - 1

        self.num_target = 4
        self.ui_xy_origin = 0., -0.9
        self.ui_xy_scale = 1.75
        self.ui_rotation = 45
        self.ui_theta_target = 22.5

        self.pipeline_mav = self.make_pipeline_mav()

        self.prepare_targets()
        self.prepare_timers()

        self.started = False

    def start_trials(self):
        if not self.started:
            self.run_trial(self.trial)    
            self.started = True

    def run(self): 
        self.calib_array = None
        self.load_calibration()

        if self.calib_array is None:
            print('No calibration array!')
        super(AbstractControl, self).run()

    def prepare_targets(self):
        if N_TARGETS == 12:
            self.radii = 0.303, 0.451, 0.673, 1
        elif N_TARGETS == 8:
            self.radii = 0.303, 0.550, 1
        elif N_TARGETS == 4:
            self.radii = 0.303, 1

        self.theta = numpy.array([((j * UI_THETA_TARGET) * (numpy.pi/180)) for j in range(4)])

        self.target_var = [(self.radii[i] * UI_XY_SCALE,
                            self.radii[i+1] * UI_XY_SCALE,
                            UI_ROTATION + j * UI_THETA_TARGET)
                           for i in range(N_TARGETS//4) for j in range(4)]

    def prepare_timers(self):
        self.iti_timer = Counter(int(TRIAL_INTERVAL / READ_LENGTH))
        self.iti_timer.timeout.connect(self.finish_iti)

        self.reach_timer = Counter(int(REACH_LENGTH / READ_LENGTH))
        self.reach_timer.timeout.connect(self.finish_reach)

        self.hold_timer = Counter(int(HOLD_LENGTH / READ_LENGTH))
        self.hold_timer.timeout.connect(self.finish_hold)

        self.score_timer = Counter(int(SCORE_LENGTH / READ_LENGTH))
        self.score_timer.timeout.connect(self.finish_trial)

    def prepare_design(self, design):
        if N_TRIALS % N_TARGETS != 0:
            print('n_trials & n_targets not balanced quitting')
            quit()
        target = numpy.repeat(numpy.arange(N_TARGETS), int(N_TRIALS / N_TARGETS))
        numpy.random.shuffle(target)

        block = design.add_block()
        for t in range(N_TRIALS):
            block.add_trial(attrs={'target': target[t]})

    def prepare_graphics(self, container):
        self.canvas = Canvas(bg_color='#000000',
                             draw_border=False)

        # Redo if finish other tasks under time constraint.
        self.t0 = gr.Marker(xy_origin=UI_XY_ORIGIN,
                            theta_target=UI_THETA_TARGET,
                            r1=self.target_var[0][0],
                            r2=self.target_var[0][1],
                            rotation=self.target_var[0][2])
        self.canvas.add_item(self.t0)

        self.t1 = gr.Marker(xy_origin=UI_XY_ORIGIN,
                            theta_target=UI_THETA_TARGET,
                            r1=self.target_var[0][0],
                            r2=self.target_var[0][1],
                            rotation=self.target_var[1][2])
        self.canvas.add_item(self.t1)

        self.t2 = gr.Marker(xy_origin=UI_XY_ORIGIN,
                            theta_target=UI_THETA_TARGET,
                            r1=self.target_var[0][0],
                            r2=self.target_var[0][1],
                            rotation=self.target_var[2][2])
        self.canvas.add_item(self.t2)

        self.t3 = gr.Marker(xy_origin=UI_XY_ORIGIN,
                            theta_target=UI_THETA_TARGET,
                            r1=self.target_var[0][0],
                            r2=self.target_var[0][1],
                            rotation=self.target_var[3][2])
        self.canvas.add_item(self.t3)

        self.t0.hide()
        self.t1.hide()
        self.t2.hide()
        self.t3.hide()

        self.cursor = Circle(diameter=0.0625 * UI_XY_SCALE,
                             color='green')
        self.cursor.hide()

        self.basket = gr.Basket(xy_origin=UI_XY_ORIGIN,
                                size=0.2 * UI_XY_SCALE,
                                xy_rotate=UI_ROTATION)
        self.basket.hide()

        self.text_score = Text(text='test', color='white')
        self.text_score.hide()

        self.canvas.add_item(self.basket)
        self.canvas.add_item(self.cursor)
        self.canvas.add_item(self.text_score)

        container.set_widget(self.canvas)

    def prepare_storage(self, storage):

        timestamp = time.strftime('%Y%m%d%H%M%S',
                                  time.localtime())
        self.writer = storage.create_task(self.folder_prefix + '_' + timestamp)
        self.data_dir = os.path.join(os.path.dirname(
                                os.path.realpath(__file__)),
                                'data')

        if (not os.path.isdir(self.data_dir)):
            os.mkdir(self.data_dir)
            
        block_savedir = os.path.join(self.data_dir, exp.subject, 'control' + '_' + timestamp)

        if (not os.path.isdir(block_savedir)):
            os.mkdir(block_savedir)

        config = ConfigParser()
        config.read("config.ini")
        with open(block_savedir+"\\config.ini", 'w') as f:
            config.write(f)

    def prepare_daq(self, daqstream):
        self.daqstream = daqstream
        self.daqstream.start()

    def run_trial(self, trial):
        self.iti_timer.reset()

        # add target to canvas
        target = self.trial.attrs['target']
        self.target = gr.Target(xy_origin=UI_XY_ORIGIN,
                                theta_target=UI_THETA_TARGET,
                                r1=self.target_var[target][0],
                                r2=self.target_var[target][1],
                                rotation=self.target_var[target][2])
        self.target.hide()
        self.canvas.add_item(self.target)

        self.trial_state = numpy.nan
        self.rest_state = numpy.nan
        self.feedback_state = numpy.nan

        # self.cursor_zone = [numpy.nan, numpy.nan]
 
        trial.add_array('data', stack_axis=1)
        trial.add_array('proc', stack_axis=1)
        trial.add_array('hold', stack_axis=1)
        trial.add_array('state', stack_axis=1)
        trial.add_array('feedback', stack_axis=1)

        # setting up arrays for each trial
        self.rest_array = numpy.array([])

        # prevents other parts of code running before update_iti has finished
        self.connect(self.daqstream.updated, self.update_iti)
        self.debug_printer.print('run trial')

    def next_trial(self):
        super(AbstractControl, self).next_trial()
        if self.started:
            self.run_trial(self.trial)

    def update_iti(self, data):
        # update state
        self.trial_state = numpy.nan
        self.hold_state = numpy.nan
        self.rest_state = 0
        if self.cursor.collides_with(self.basket):
            self.rest_state = 1
        
        # update default
        self.update_default(data)
        # update timer
        self.iti_timer.increment()

    def update_default(self, data):
        # processed mav data 
        proc_data = self.pipeline_mav(data)

        # histogram based normalisation
        norm_proc_data = self.apply_normalisation(proc_data)      
        cursor_pos = norm_proc_data[self.ctrl_channels, -1]

        # get cursor zone for presenting feedback
        self.get_cursor_zone(cursor_pos)

        self.cursor.pos = self.transform_data(cursor_pos)        
        self.trial.arrays['hold'].stack(self.hold_state)
        self.trial.arrays['state'].stack(self.trial_state)
        self.trial.arrays['feedback'].stack(self.feedback_state)

        self.trial.arrays['data'].stack(data)
        self.trial.arrays['proc'].stack(norm_proc_data)
        # self.trial.arrays['proc'].stack(numpy.transpose(norm_proc_data))

        self.rest_array = numpy.append(self.rest_array, self.rest_state)

    def update_rest(self, data):
        # update graphics
        self.cursor.show()
        # update state
        self.trial_state = 0
        self.hold_state = numpy.nan
        self.rest_state = 0
        if self.cursor.collides_with(self.basket):
            self.rest_state = 1
        # update default
        self.update_default(data)
        # update status
        if numpy.all(self.rest_array[-int(WIN_SIZE_REST / READ_LENGTH):]):
            self.finish_rest()

    def update_reach(self, data):
        # update state
        self.trial_state = 1
        self.hold_state = 0
        self.rest_state = numpy.nan
        if self.cursor.collides_with(self.target):
            self.hold_state = 1

        # update default
        self.update_default(data)
        # update timer
        self.reach_timer.increment()

    def update_hold(self, data):
        # update state
        self.trial_state = 2
        self.hold_state = 0
        self.rest_state = numpy.nan
        if self.cursor.collides_with(self.target):
            self.hold_state = 1

        # update default
        self.update_default(data)
        # update timer
        self.hold_timer.increment()

    def update_score(self, data):
        # update state
        self.trial_state = numpy.nan
        self.hold_state = numpy.nan
        self.rest_state = numpy.nan
        # update default
        self.update_default(data)
        # update timer
        self.score_timer.increment()

    def get_cursor_zone(self, cursor_pos):
        # simplest way of getting targets is to use polar space
        _rad = numpy.hypot(cursor_pos[0], cursor_pos[1])  
        _theta = numpy.arctan2(cursor_pos[1], cursor_pos[0])

        '''
        only checking simple radii :. will only work with four targets
        '''

        # get current zone
        if _rad < self.radii[0]:
            _cursor_zone = -1
        else:
            _cursor_zone = numpy.argwhere(self.theta <= _theta)[-1][0]

        # if change of zone or new trial
        if _cursor_zone != self.feedback_state:
            # send ble command - should be exposed somewhere more sensible
            if self.ble_feedback.connected:
                self.ble_feedback.send(_cursor_zone)
                
        # update current zone
        self.feedback_state = _cursor_zone

    def finish_iti(self):
        self.basket.show()
        self.disconnect(self.daqstream.updated, self.update_iti)
        self.connect(self.daqstream.updated, self.update_rest)
     
    def finish_rest(self):
        self.cursor.show()
        self.play_beep()
 
        self.t0.show()
        self.t1.show()
        self.t2.show()
        self.t3.show()

        self.target.show()
        self.reach_timer.reset()

        self.disconnect(self.daqstream.updated, self.update_rest)
        self.connect(self.daqstream.updated, self.update_reach)

    def finish_reach(self):
        self.hold_timer.reset()
        self.disconnect(self.daqstream.updated, self.update_reach)
        self.connect(self.daqstream.updated, self.update_hold)

    def finish_hold(self):
        self.cursor.hide()
        #self.cursor.show()
        
        self.t0.hide()
        self.t1.hide()
        self.t2.hide()
        self.t3.hide()

        self.basket.hide()
        self.target.hide()
        self.cursor.hide()

        # calculate score
        hold_period = numpy.where(self.trial.arrays['state'].data == 2)[0]
        self.score = numpy.mean(self.trial.arrays['hold'].data[hold_period])
        score_text = "{:.0f} %".format(self.score * 100)
        self.text_score.qitem.setText(score_text)
        self.text_score.show()

        self.disconnect(self.daqstream.updated, self.update_hold)
        self.connect(self.daqstream.updated, self.update_score)

        self.debug_printer.print('finish hold')

    def finish_trial(self):
        self.text_score.hide()
        self.trial.attrs['percent_hold'] = self.score
        self.writer.write(self.trial)
        self.disconnect(self.daqstream.updated, self.update_score)
        self.next_trial()

    def finish(self):

        '''
        self.trial? 
        '''



        self.end_block()
        self.disconnect_all()
        self.ble_feedback.stop()

    def end_block(self):
        self.daqstream.stop()
        self.finished.emit()

    def key_press(self, key):
        if key == util.key_escape:
            #self.finish() #to remove later
            self.end_block()
        else:
            super().key_press(key)

        # trial    
        if key == util.key_space:
            self.start_trials()

    def play_beep(self):
        winsound.PlaySound('sounds\\beep.wav', 1)
        self.debug_printer.print('play beep')

    def transform_data(self, pos):
        "New position after rotation and translation. Based on interface"
        x = pos[0]
        y = pos[1]

        x_new = x * UI_XY_SCALE * numpy.cos(numpy.radians(UI_ROTATION)) - \
            y * UI_XY_SCALE * numpy.sin(numpy.radians(UI_ROTATION)) + \
            UI_XY_ORIGIN[0]
        y_new = x * UI_XY_SCALE * numpy.sin(numpy.radians(UI_ROTATION)) + \
            y * UI_XY_SCALE * numpy.cos(numpy.radians(UI_ROTATION)) + \
            UI_XY_ORIGIN[1]

        return x_new, y_new

    ''' calib_array added to load calibdation data '''
    def reset_calib_array(self):
        self.calib_array = numpy.ones((N_CHANNELS, 3)) * numpy.nan


if __name__ == '__main__':

    parser = ArgumentParser()
    cond = parser.add_argument_group()
    cond.add_argument('--develop', action='store_true')

    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument('--config', action='store_true')
    task.add_argument('--task', action='store_true')
    task.add_argument('--keys', action='store_true')

    args = parser.parse_args()
    cp = ConfigParser(allow_no_value=False)

    # Read config file
    cp.read(os.path.join(os.path.dirname(os.path.realpath(__file__)),
            "config.ini"))

    # Specific parameters
    SUBJECT = cp.get('experiment', 'subject')
  
    # General parameters
    DISPLAY_WIN_SIZE = cp.getfloat('display', 'win_size')
    DISPLAY_COLUMNS = cp.getint('display', 'columns')
    DISPLAY_UPDATE_RATIO = cp.getint('display', 'update_read_ratio')
    
    NORMALISE_Y_RANGE_MIN = cp.getfloat('normalisation', 'y_range_min')
    NORMALISE_Y_RANGE_MAX = cp.getfloat('normalisation', 'y_range_max')
    NORMALISE_Y_RANGE_INCREMENT = cp.getfloat('normalisation', 'y_range_increment')
    NORMALISE_Y_RANGE_DEFAULT = cp.getfloat('normalisation', 'y_range_default')
    NORMALISE_PERC = cp.getfloat('normalisation', 'normalise_perc')
    
    MAX_TRIAL_LENGTH = 60 * 5
    MAX_N_TRIALS = 100
    DISPLAY_RECORD_RATIO = 10

    # Monitor; must use fullscreen or maxscreen 
    MONITOR = 0
    FULLSCREEN = False
    MAXSCREEN = False
    LEFTSCREEN = True
    RIGHTSCREEN = False

    # Device parameters
    N_CHANNELS = cp.getint('device', 'channels') 
    S_RATE = cp.getint('device', 'sampling_rate')
    READ_LENGTH = cp.getfloat('device', 'read_length')

    # Stimulator parameters
    STIM_DEVICE_NAME = cp.get('stimulator', 'device_name')
    STIM_PORT = cp.get('stimulator', 'port')
    if not STIM_PORT:
        STIM_PORT = None

    '''
    #
    # Getting serial port of a COM device in Windows
    #
    
    from serial import Serial, SerialException
    from serial.tools import list_ports

    def get_serial_port(self):
    device = None
    comports = list_ports.comports()
    for port in comports:
        if port.description.startswith(self.name):
            device = port.device
    if device is None:
        raise Exception("Serial COM port not found.")
    else:
        return device 
    '''

    # Filtering
    MAV_WIN_SIZE = cp.getfloat('filter', 'win_size')

    # Experiment parameters which may change
    CTRL_CHANNELS = list(map(int, (cp.get('experiment', 'channels').split(','))))
    N_TRIALS = cp.getint('experiment', 'n_trials')
    N_TARGETS = cp.getint('experiment', 'n_targets')
    WIN_SIZE_REST = cp.getfloat('experiment', 'win_size_rest')
    TRIAL_INTERVAL = cp.getfloat('experiment', 'trial_interval')
    REACH_LENGTH = cp.getfloat('experiment', 'reach')
    HOLD_LENGTH = cp.getfloat('experiment', 'hold')
    SCORE_LENGTH = cp.getfloat('experiment', 'score_present')

    # Interface settings
    UI_XY_ORIGIN = list(map(float, (cp.get('interface', 'xy_origin').split(','))))
    UI_XY_SCALE = cp.getfloat('interface', 'xy_scale')
    UI_ROTATION = cp.getint('interface', 'rotation')
    UI_THETA_TARGET = cp.getfloat('interface', 'theta_target')

    # messy to fix later
    DISPLAY_CHANNEL_NAMES = ['C' + str(i) for i in range(1, N_CHANNELS + 1)]


    keyString = \
    '''
        Esc:        Quit
        Space:      Record on/off
        WSAD:       Channel selection
        
        N:          Histogram normalise channel on/off
        M:          Reset channel histogram

        O:          Histogram normalise all channel on/off
        P:          Reset all channel histogram
            
        Z:          Save calibration
        X:          Load calibration

        I:          Autoscaling on/off
        U:          Increase Y range
        Y:          Decrease Y range
    '''

    if args.keys:
        print(keyString)
        exit()

    if args.develop:
        # hacked now in the code
        dev_myo = FakeMyo(rate=S_RATE, read_length=READ_LENGTH)
    else:
        myo_sdk_path = str(pathlib.Path(__file__).parent.resolve()) + '\sdk\myo-sdk-win-0.9.0'
        myo.init(sdk_path=myo_sdk_path)
        dev_myo = MyoEMG(channels=range(N_CHANNELS), samples_per_read=10)
        
    if args.config:
        print(keyString)
        exp = MyExperiment(daq=dev_myo, subject=SUBJECT, allow_overwrite=True)
        exp.run(RecordingScopeTask(folder_prefix="config"))

    if args.task:
        exp = MyExperiment(daq=dev_myo, subject=SUBJECT, allow_overwrite=True)
        exp.run(AbstractControl(folder_prefix="control"))

    exit()