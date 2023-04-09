#!/usr/bin/env python
 
# import normal packages
import platform 
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file
from requests.auth import HTTPDigestAuth
 
# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusTasmotaService:
  def __init__(self, servicename, paths, productname='Tasmota', connection='Tasmota HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    customname = config['DEFAULT']['CustomName']

    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
    self._paths = paths
    
    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
    
    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)
    
    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    #self._dbusservice.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
    self._dbusservice.add_path('/ProductId', 0xFFFF) # id assigned by Victron Support from SDM630v2.py
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', customname)    
    self._dbusservice.add_path('/Connected', 1)
    
    self._dbusservice.add_path('/Latency', None)    
    self._dbusservice.add_path('/FirmwareVersion', 0.1)
    self._dbusservice.add_path('/HardwareVersion', 0)
    self._dbusservice.add_path('/Position', 0) # normaly only needed for pvinverter
    self._dbusservice.add_path('/Serial', self._getTasmotaSerial())
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path('/StatusCode', 0)  # Dummy path so VRM detects us as a PV-inverter.
    
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    gobject.timeout_add(1001, self._update) # pause 250ms before the next request
    
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)
 
  def _getTasmotaSerial(self):
    config = self._getConfig()                                                                                                                                                                                               
    serial = config['DEFAULT']['Serial']
    return serial
 
 
  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config;
 
 
  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']
    
    if not value: 
        value = 0
    
    return int(value)
  
  
  def _getTasmotaStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise':
        URL = "http://%s:%s@%s/cm?cmnd=status%%208" % (config['ONPREMISE']['Username'], config['ONPREMISE']['Password'], config['ONPREMISE']['Host'])
        URL = URL.replace(":@", "")
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
    
 
  def _getTasmotaData(self):
    config = self._getConfig()
    customName = config['DEFAULT']['CustomName']


    URL = self._getTasmotaStatusUrl()
    meter_r = requests.get(url = URL)
    
    # check for response
    if not meter_r:
        raise ConnectionError("No response from Tasmota - %s" % (URL))
    
    meter_data = meter_r.json()     
    
    # check for Json
    if not meter_data['StatusSNS'][customName]:
        raise ValueError("Converting response to JSON failed")
    
    
    return meter_data['StatusSNS'][customName]
 
 
  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("--- End: sign of life ---")
    return True
 
  def _update(self):   
    try:
       #get data from Tasmota
       meter_data = self._getTasmotaData()
       timestamp = int(str(meter_data['yy']) + str(meter_data['MM']).zfill(2) + str(meter_data['dd']).zfill(2) + str(meter_data['hh']).zfill(2) + str(meter_data['mm']).zfill(2) + str(meter_data['ss']).zfill(2))

       if meter_data['status'] != 1:
        self._dbusservice['/Connected'] = 0
        return True
       self._dbusservice['/Connected'] = 1

      if self._lastUpdate >= timestamp:
        return True

       config = self._getConfig()

       #send data to DBus
       for phase in ['L1', 'L2', 'L3']:
         pre = '/Ac/' + phase

         power = meter_data[phase + '_w']
         voltage = meter_data[phase + '_v']
         current = meter_data[phase + '_c']
           
         self._dbusservice[pre + '/Voltage'] = voltage
         self._dbusservice[pre + '/Current'] = current
         self._dbusservice[pre + '/Power'] = power
         self._dbusservice[pre + '/Energy/Forward'] = power/1000
           
       self._dbusservice['/Ac/Power'] = meter_data['mainWatt']
       self._dbusservice['/Ac/Energy/Forward'] = meter_data['mainWatt']/1000

       #logging
       logging.debug("House Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
       logging.debug("House Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
       logging.debug("---");
       
       # increment UpdateIndex - to show that new data is available
       index = self._dbusservice['/UpdateIndex'] + 1  # increment index
       if index > 255:   # maximum value of the index
         index = 0       # overflow from 255 to 0
       self._dbusservice['/UpdateIndex'] = index

       #update lastupdate vars
       self._lastUpdate = timestamp             
    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True
 
  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change
 


def getLogLevel():
  config = configparser.ConfigParser()
  config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
  logLevelString = config['DEFAULT']['LogLevel']
  
  if logLevelString:
    level = logging.getLevelName(logLevelString)
  else:
    level = logging.INFO
    
  return level


def main():
  #configure logging
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=getLogLevel(),
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
  
 
  try:
      logging.info("Start");
  
      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      #formatting 
      _kwh = lambda p, v: (str(round(v, 2)) + ' kWh')
      _a = lambda p, v: (str(round(v, 2)) + ' A')
      _w = lambda p, v: (str(round(v, 1)) + ' W')
      _v = lambda p, v: (str(round(v, 1)) + ' V')  
     
      #start our main-service
      pvac_output = DbusTasmotaService(
        servicename='com.victronenergy.pvinverter',
        paths={
          '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh}, # energy produced by pv inverter
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          
          '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L2/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L3/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L2/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L3/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L2/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L3/Energy/Forward': {'initial': None, 'textformat': _kwh},
        })
     
      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()            
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
