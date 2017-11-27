// Code goes here
window.onload = function() {

    var canvas = document.getElementById('screenCanvas');
    var statusBar = document.getElementById('statusBar');
    var actionList = document.getElementById('actionList');
    canvas.addEventListener('mousemove', handleMouseMove, false);
    canvas.addEventListener('mousedown', handleMouseDown, false);
    canvas.addEventListener('mouseup', handleMouseUp, false);
    canvas.addEventListener('mouseleave', handleMouseLeave, false);

    // keep a record of previous mouse event
    var lastMouseDownEvent = {timestamp: 0, x: 0, y: 0};
    const LONG_PRESS_THRESHOLD = 500 // mouse down time > this threshold is considered as long press.
    const ACTION_REQUEST_BASE_URL = '/action'
    const TIMESTAMP_REQUEST_URL = '/timestamp'
    const REDRAW_INTERVAL = 1000 // redraw every 1 second
    const SCREEN_IMAGE_URI = 'imgs/t.png'
    const MAX_SCREEN_BOX_SIZE = 960 // the widest a screen box can be

    // redraw the image on a timer
    
    var context = canvas.getContext("2d");
    var screenImg = new Image();
    var scale = 1; // define the scale of phone screen:canvas size
    var canvasWidth = 500;
    var canvasHeight = 500;
    var lastTimestamp = ''; // The timestamp of last screencap.

    screenImg.onload = function() {
      // need to calculate the image scale
      maxScreenSize = Math.max(screenImg.width, screenImg.height);
      if (maxScreenSize > MAX_SCREEN_BOX_SIZE) {
        scale = MAX_SCREEN_BOX_SIZE / maxScreenSize
      } else {
        scale = 1
      }
      canvasWidth = Math.round(screenImg.width * scale);
      canvasHeight = Math.round(screenImg.height * scale);

      canvas.setAttribute('width', canvasWidth);
      canvas.setAttribute('height', canvasHeight);
      context.drawImage(screenImg, 0, 0, canvasWidth, canvasHeight);
      var tsFloat = parseFloat(lastTimestamp);
      var tsDate = new Date(tsFloat * 1000);
      context.font = "12px Arial";
      context.fillText('@' + tsDate.toLocaleTimeString(), 200, 20);
    }
    
    redrawScreen();

    function redrawScreen() {
      var xmlHttp = new XMLHttpRequest();
      xmlHttp.open("GET", TIMESTAMP_REQUEST_URL, false) // send request sync
      xmlHttp.send( null );
      ts = xmlHttp.responseText;
      if (ts != lastTimestamp) {
        screenImg.src = SCREEN_IMAGE_URI+ '?d=' + getCurrentTimestamp();
        lastTimestamp = ts;
      }
      setTimeout(redrawScreen, REDRAW_INTERVAL);
    }

    function getCurrentTimestamp() {
      return Date.now()
    }

    function handleMouseMove(evt) {
      var mousePos = getMousePos(canvas, evt);
      var message = 'Mouse position: ' + mousePos.x + ',' + mousePos.y;
      writeMessage(statusBar, message);
    }

    function handleMouseDown(evt) {
      var mousePos = getMousePos(canvas, evt);
      writeMessage(statusBar, 'Mouse down: ' + mousePos.x + ',' + mousePos.y);
      lastMouseDownEvent.timestamp = getCurrentTimestamp();
      lastMouseDownEvent.x = mousePos.x;
      lastMouseDownEvent.y = mousePos.y;
    }

    function handleMouseUp(evt) {
      var mousePos = getMousePos(canvas, evt);
      writeMessage(statusBar, 'Mouse up: ' + mousePos.x + ',' + mousePos.y);
      mouseUpEvt = {
        timestamp: getCurrentTimestamp(), 
        x: mousePos.x,
        y: mousePos.y
      };
      performAction(lastMouseDownEvent, mouseUpEvt);
      // clear the previous mouse down event.
      lastMouseDownEvent = {timestamp: 0, x: 0, y: 0};
    }

    // Treat mouse leave the same as mouse up.
    function handleMouseLeave(evt) {
      handleMouseUp(evt);
    }

    function writeMessage(status, message) {
      statusBar.innerHTML = message;
    }

    function getMousePos(canvas, evt) {
      var rect = canvas.getBoundingClientRect();
      return {
        x: Math.round((evt.clientX - rect.left)/scale),
        y: Math.round((evt.clientY - rect.top)/scale)
       };
    }

    // generate actions based on the last mouse down/up event.
    function performAction(mouseDownEvt, mouseUpEvt) {
      // timestamp must be valid
      if (mouseDownEvt.timestamp <= 0 || mouseUpEvt.timestamp <= 0 ||
          mouseUpEvt.timestamp < mouseDownEvt.timestamp) {
          return;
      }
      deltaT = mouseUpEvt.timestamp - mouseDownEvt.timestamp;
      sendSwipeAction(mouseDownEvt.x, mouseDownEvt.y, mouseUpEvt.x, mouseUpEvt.y, deltaT);
      logAction(actionList, 'swipe', mouseDownEvt.x, mouseDownEvt.y, mouseUpEvt.x, mouseUpEvt.y, deltaT);
    }

    function logAction(list, act, x1, y1, x2, y2, t) {
      // only need swipe action for now
      var actionMsg = act
      actionMsg += ' (' + x1 + ',' + y1 + ')-(' + x2 + ',' + y2 + '), ' + t + 'ms';
      option = document.createElement('option');
      option.text = actionMsg;
      actionList.add(option, list[0]);
    }

    // Send swipe action http request
    function sendSwipeAction(x1, y1, x2, y2, t) {
      var url = ACTION_REQUEST_BASE_URL + '?act=swipe'
      url += '&x1=' + x1 + '&y1=' + y1;
      url += '&x2=' + x2 + '&y2=' + y2;
      url += '&t=' + t
      var xmlHttp = new XMLHttpRequest();
      xmlHttp.open("GET", url, true) // send request async
      xmlHttp.send( null );
    }
}