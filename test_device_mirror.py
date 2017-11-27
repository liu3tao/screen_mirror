import time
from device_mirror import device_mirror

def test_get_screencap(dm):
  cap = dm.get_screencap()
  if cap is not None:
    print('cap size %d' % len(cap))
    return True
  else:
    print('Get cap failed')
    return False
    

def test_send_swipe(dm):
  ret = dm.send_action('swipe', 1358, 680, 1358, 680, 100)
  print('Ret=%s' % ret)
  return True

if __name__ == '__main__':
  dm = device_mirror()
  dm.run()
  time.sleep(2)
  test_get_screencap(dm)
  test_send_swipe(dm)

