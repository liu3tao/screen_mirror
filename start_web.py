

import time
import argparse
import flask
from device_mirror import device_mirror

parser = argparse.ArgumentParser()
parser.add_argument('-s', '--serial', help="Phone's serial number", default=None)
parser.add_argument('-i', '--interval', help='Time interval between screencap',
                    type=float, default=0.5)

def create_app():
  """Get command line args and create objects"""
  args = parser.parse_args()
  app = flask.Flask(__name__)
  print 'serial:', args.serial
  print 'interval:', args.interval
  app.config['SOURCE'] = device_mirror(args.serial, args.interval)
  return app

app = create_app()

@app.route('/')
def index():
  return flask.render_template('index.html')

@app.route('/action', methods=['GET'])
def perform_action():
  res = 'Failed.'
  act = flask.request.args.get('act')
  x1 = flask.request.args.get('x1')
  y1 = flask.request.args.get('y1')
  x2 = flask.request.args.get('x2')
  y2 = flask.request.args.get('y2')
  t = flask.request.args.get('t')
  if None in (act, x1, y1, x2, y2, t):
    print('Invalid action request, URL=%s' % flask.request.url)
    flask.abort(400) #  returns a bad request error code.
  else:
    dm = flask.current_app.config['SOURCE']
    if dm.send_action(act, x1, y1, x2, y2, t):
      return 'Action Success'
    else:
      return 'Action Failed'

@app.route('/imgs/<path>')
def images(path):
  dm = flask.current_app.config['SOURCE']
  img = dm.get_screencap()
  resp = flask.make_response(img)
  resp.content_type = 'image/png'
  return resp

@app.route('/timestamp')
def get_timestamp():
  dm = flask.current_app.config['SOURCE']
  return str(dm.get_screencap_timestamp())


if __name__ == '__main__':
  dm = app.config['SOURCE']
  print('Starting adb screen cap thread...')
  dm.run()
  # wait till first screen cap is ready
  while dm.is_capturing and dm.get_screencap_timestamp() == 0:
    time.sleep(0.5)
  if not dm.is_capturing:
    print('ADB screencap thread failed to start.')
  else:
    print('ADB screencap thread started.')
    print('Web server started at http://127.0.0.1:5000')
    app.run()
