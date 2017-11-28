import sys
import time
import logging
from mobly.controllers.android_device_lib import adb
from threading import Thread, RLock

class device_mirror(object):
  def __init__(self, serial=None, interval=0.5):
    self.serial = serial  # phone's serial number
    self.interval = interval  # interval between screencap.
    self.adb = adb.AdbProxy(serial)
    self.screencap_cmd = 'screencap -p '
    self.screencap_buffer = None
    self.screencap_timestamp = 0
    self.is_capturing = False
    self._lock = RLock()
    self._local_file = '/tmp/screencap.png'
    self._remote_file = '/data/local/tmp/screencap.png'

  def _take_screencap(self):
    cmd = self.screencap_cmd + self._remote_file
    self.adb.shell(cmd)
    self.adb.pull([self._remote_file, self._local_file])
    img = None
    with open(self._local_file, 'rb') as f:
      img = f.read()
    return img

  def _screencap_thread_func(self):
    self.is_capturing = True
    while self.is_capturing:
      try:
        cap = self._take_screencap()
        if cap:
          # update screen buffer
          self._lock.acquire()
          self.screencap_buffer = cap
          self.screencap_timestamp = time.time()
          self._lock.release()
          # print('New screencap: %d bytes, ts=%f' % (len(self.screencap_buffer), self.screencap_timestamp))
      except adb.AdbError as e:
        print('Caught ADB error: %s' % e)
        self.is_capturing = False
      time.sleep(self.interval)

  def get_screencap_timestamp(self):
    """Return latest screencap timestamp"""
    self._lock.acquire()
    ts = self.screencap_timestamp
    self._lock.release()
    return ts

  def get_screencap(self):
    self._lock.acquire()
    buf = self.screencap_buffer
    self._lock.release()
    return buf

  def send_action(self, act, x1, y1, x2, y2, t):
    """Send shell input command."""
    cmd = ' '.join(str(x) for x in ['input', act, x1, y1, x2, y2, t])
    try:
      self.adb.shell(cmd)
    except adb.AdbError as e:
      print('send_action: Caught ADB error: %s' % e)
      return False
    return True

  def run(self):
    self.screencap_thread = Thread(target=self._screencap_thread_func)
    self.screencap_thread.start()
    # self.screencap_thread.join()
