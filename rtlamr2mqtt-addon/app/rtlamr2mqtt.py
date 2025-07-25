#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rtlamr2mqtt - A Home Assistant add-on for RTLAMR
https://github.com/natekspencer/rtlamr2mqtt/blob/main/LICENSE

This add-on uses the code from:
- https://github.com/bemasher/rtlamr
- https://git.osmocom.org/rtl-sdr
"""

import os
import sys
import logging
import subprocess
import signal
from datetime import datetime
from json import dumps
from time import sleep
from shutil import which
import helpers.config as cnf
import helpers.buildcmd as cmd
import helpers.mqtt_client as m
import helpers.ha_messages as ha_msgs
import helpers.read_output as ro
import helpers.usb_utils as usbutil
import helpers.info as i


# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(format='[%(asctime)s] %(levelname)s:%(message)s', level=logging.DEBUG)
LOG_LEVEL = 0
logger.info('Starting rtlamr2mqtt %s', i.version())



def shutdown(rtlamr=None, rtltcp=None, mqtt_client=None, base_topic='rtlamr', offline=False):
    """ Shutdown function to terminate processes and clean up """
    if LOG_LEVEL >= 3:
        logger.info('Shutting down...')
    # Terminate RTLAMR
    if rtlamr is not None:
        if LOG_LEVEL >= 3:
            logger.info('Terminating RTLAMR...')
        rtlamr.stdout.close()
        rtlamr.terminate()
        try:
            rtlamr.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            rtlamr.kill()
            rtlamr.communicate()
        if LOG_LEVEL >= 3:
            logger.info('RTLAMR Terminated.')
    # Terminate RTL_TCP
    if rtltcp not in [None, 'remote']:
        if LOG_LEVEL >= 3:
            logger.info('Terminating RTL_TCP...')
        rtltcp.stdout.close()
        rtltcp.terminate()
        try:
            rtltcp.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            rtltcp.kill()
            rtltcp.communicate()
        if LOG_LEVEL >= 3:
            logger.info('RTL_TCP Terminated.')
    if mqtt_client is not None and offline:
        mqtt_client.publish(
            topic=f'{base_topic}/status',
            payload='offline',
            qos=1,
            retain=False
        )
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    if LOG_LEVEL >= 3:
        logger.info('All done. Bye!')



def signal_handler(signum, frame):
    """ Signal handler for SIGINT and SIGTERM """
    raise RuntimeError(f'Signal {signum} received.')



def get_iso8601_timestamp():
    """
    Get the current timestamp in ISO 8601 format
    """
    return datetime.now().astimezone().replace(microsecond=0).isoformat()



def start_rtltcp(config):
    """ Start RTL_TCP process """
    # Check if we are using a remote RTL_TCP server
    is_remote = config["general"]["rtltcp_host"].split(':')[0] not in [ '127.0.0.1', 'localhost' ]

    if is_remote:
        return 'remote'

    if 'RTLAMR2MQTT_USE_MOCK' in dict(os.environ) or is_remote:
        usb_id_list = [ '001:001']
    else:
        # Search for RTL-SDR devices
        usb_id_list = usbutil.find_rtl_sdr_devices()

    usb_id = config['general']['device_id']
    if config['general']['device_id'] == '0':
        if len(usb_id_list) > 0:
            usb_id = usb_id_list[0]
        else:
            logger.critical('No RTL-SDR devices found. Exiting...')
            return None


    if 'RTLAMR2MQTT_USE_MOCK' not in dict(os.environ) and not is_remote:
        if LOG_LEVEL >= 3:
            logger.debug('Reseting USB device: %s', usb_id)
        usbutil.reset_usb_device(usb_id)

    rtltcp_args = cmd.build_rtltcp_args(config)
    rtltcp_full_command = [which("rtl_tcp")] + rtltcp_args

    if LOG_LEVEL >= 3:
        logger.info('Starting RTL_TCP using: %s', " ".join(rtltcp_full_command))

    try:
        # rtltcp = subprocess.Popen(["strace", "--output=out.trace", "rtl_tcp"] + rtltcp_args,
        rtltcp = subprocess.Popen(["/usr/bin/unbuffer"] + rtltcp_full_command,
            start_new_session=True,
            text=True,
            close_fds=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1)

        os.set_blocking(rtltcp.stdout.fileno(), False)

    except Exception as e:
        logger.critical('Failed to start RTL_TCP. %s', e)
        return None

    rtltcp_is_ready = False
    # Wait for rtl_tcp to be ready

    while not rtltcp_is_ready:
        try:
            rtltcp_output = rtltcp.stdout.readline().strip()
        except Exception as e:
            logger.critical(e)
            return None
        if rtltcp_output:
            if LOG_LEVEL >= 4:
                logger.debug(rtltcp_output)
            if "listening..." in rtltcp_output:
                rtltcp_is_ready = True
                if LOG_LEVEL >= 3:
                    logger.info('RTL_TCP has started!')
        # Check rtl_tcp status
        rtltcp.poll()
        if rtltcp.returncode is not None:
            logger.critical('RTL_TCP failed to start errcode: %d', int(rtltcp.returncode))
            return None

    return rtltcp



def start_rtlamr(config):
    """ Start RTLAMR process """
    rtlamr_args = cmd.build_rtlamr_args(config)
    rtlamr_full_command = [which("rtlamr")] + rtlamr_args

    # Tickle the rtl_tcp server to wake it up
    usbutil.tickle_rtl_tcp(config['general']['rtltcp_host'])

    if LOG_LEVEL >= 3:
        logger.info('Starting RTLAMR using: %s', " ".join(rtlamr_full_command))
    try:
        rtlamr = subprocess.Popen(["/usr/bin/unbuffer"] + rtlamr_full_command,
            close_fds=True,
            text=True,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1)
        os.set_blocking(rtlamr.stdout.fileno(), False)

    except Exception:
        logger.critical('Failed to start RTLAMR. Exiting...')
        return None

    rtlamr_is_ready = False
    while not rtlamr_is_ready:
        try:
            rtlamr_output = rtlamr.stdout.readline().strip()
        except Exception as e:
            logger.critical(e)
            rtlamr_is_ready = False
            return None
        if rtlamr_output:
            if LOG_LEVEL >= 4:
                logger.debug(rtlamr_output)
            if 'GainCount:' in rtlamr_output:
                rtlamr_is_ready = True
                if LOG_LEVEL >= 3:
                    logger.info('RTLAMR has started!')
        # Check rtl_tcp status
        rtlamr.poll()
        if rtlamr.returncode is not None:
            logger.critical('RTLAMR failed to start errcode: %d', rtlamr.returncode)
            return None

    return rtlamr



def main():
    """
    Main function
    """
    # Signal handlers/call back
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Load the configuration file
    if len(sys.argv) == 2:
        config_path = os.path.join(os.path.dirname(__file__), sys.argv[1])
    else:
        config_path = None
    err, msg, config = cnf.load_config(config_path)

    if err != 'success':
        # Error loading configuration file
        logger.critical(msg)
        sys.exit(1)
    # Configuration file loaded successfully
    # Use LOG_LEVEL as a global variable
    global LOG_LEVEL
    # Convert verbosity to a number and store as LOG_LEVEL
    LOG_LEVEL = ['none', 'error', 'warning', 'info', 'debug'].index(config['general']['verbosity'])
    if LOG_LEVEL >= 3:
        logger.info(msg)
    ##################################################################

    # ToDo:
    # Here is were it will be defined how the code will search
    # for a meter_id based on a value.
    # res = list((sub for sub in config['meters'] if config['meters'][sub]['name'][-7:] == "_FINDME"))

    # Get a list of meters ids to watch
    meter_ids_list = set(config['meters'].keys())

    # Create MQTT Client and connect to the broker
    mqtt_client = m.MQTTClient(
        broker=config['mqtt']['host'],
        port=config['mqtt']['port'],
        username=config['mqtt']['user'],
        password=config['mqtt']['password'],
        tls_enabled=config['mqtt']['tls_enabled'],
        tls_insecure=config['mqtt']['tls_insecure'],
        ca_cert=config['mqtt']['tls_ca'],
        client_cert=config['mqtt']['tls_cert'],
        client_key=config['mqtt']['tls_keyfile'],
        log_level=LOG_LEVEL,
        logger=logger,
    )

    # Set Last Will and Testament
    mqtt_client.set_last_will(
        topic=f'{config["mqtt"]["base_topic"]}/status',
        payload="offline",
        qos=1,
        retain=False
    )

    try:
        mqtt_client.connect()
    except Exception as e:
        logger.critical('Failed to connect to MQTT broker: %s', e)
        sys.exit(1)

    # Set on_message callback
    # mqtt_client.set_on_message_callback(on_message)

    # Subscribe to Home Assistant status topic
    mqtt_client.subscribe(config['mqtt']['ha_status_topic'], qos=1)

    # Start the MQTT client loop
    mqtt_client.loop_start()


    def publish_discovery_if_new(meter_id):
        if meter_id not in meter_ids_list:
            logger.debug("Discovered new meter: %s", meter_id)
            meter_ids_list.add(meter_id)

            meter_config = {
                "name": f"Meter {meter_id}",
                "id": meter_id,
                "state_class": "total_increasing",
                # Optional: add 'device_class', 'unit_of_measurement', etc.
            }

            discovery_payload = ha_msgs.meter_discover_payload(config["mqtt"]["base_topic"], meter_config)
            mqtt_client.publish(
                topic=f'{config["mqtt"]["ha_autodiscovery_topic"]}/device/{meter_id}/config',
                payload=dumps(discovery_payload),
                qos=1,
                retain=False
            )
            sleep(1) # sleep to allow discovery to process

    # Publish the discovery messages for all meters
    for meter in config['meters']:
        discovery_payload = ha_msgs.meter_discover_payload(config["mqtt"]["base_topic"], config['meters'][meter])
        mqtt_client.publish(
            topic=f'{config["mqtt"]["ha_autodiscovery_topic"]}/device/{meter}/config',
            payload=dumps(discovery_payload),
            qos=1,
            retain=False
        )

    # Give some time for the MQTT client to connect and publish
    sleep(1)
    # Publish the initial status
    mqtt_client.publish(
        topic=f'{config["mqtt"]["base_topic"]}/status',
        payload='online',
        qos=1,
        retain=False
    )

    ##################################################################
    # Is rtl_tcp configured to run on a remote host?
    is_rtltcp_remote = config["general"]["rtltcp_host"].split(':')[0] not in [ '127.0.0.1', 'localhost' ]
    #
    rtltcp = None
    rtlamr = None
    keep_reading = True
    read_counter = []
    while keep_reading:
        try:
            if mqtt_client.last_message is not None:
                if LOG_LEVEL >= 3:
                    logger.debug('Received MQTT message: %s on topic %s',
                        mqtt_client.last_message.payload.decode(),
                        mqtt_client.last_message.topic
                    )
                    for meter in config['meters']:
                        discovery_payload = ha_msgs.meter_discover_payload(config["mqtt"]["base_topic"], config['meters'][meter])
                        mqtt_client.publish(
                            topic=f'{config["mqtt"]["ha_autodiscovery_topic"]}/device/{meter}/config',
                            payload=dumps(discovery_payload),
                            qos=1,
                            retain=False
                        )
                mqtt_client.last_message = None

            # Start RTL_TCP if not remote
            if not is_rtltcp_remote:
                if rtltcp is None:
                    rtltcp = start_rtltcp(config)
                if rtltcp is not None:
                    rtltcp.poll()
                if rtltcp.returncode is not None:
                    if LOG_LEVEL >= 3:
                        logger.critical('RTL_TCP has died, trying to restart...')
                    rtltcp = start_rtltcp(config)
                    if rtltcp is not None:
                        rtltcp.poll()
                if rtltcp is None:
                    logger.critical('Failed to start RTL_TCP. Exiting...')
                    shutdown(
                                rtlamr=None,
                                rtltcp=rtltcp,
                                mqtt_client=mqtt_client,
                                base_topic=config['mqtt']['base_topic'],
                                offline=True
                            )
                    sys.exit(1)
            else:
                logger.info('Using remote RTL_TCP server at %s', config['general']['rtltcp_host'])
                # If we are using a remote RTL_TCP server, we can skip the rest of the setup
                # and just read from the remote server
                rtltcp = None

            ##################################################################

            # Read the output from RTLAMR
            # Start RTLAMR if it is not already running
            if rtlamr is None:
                rtlamr = start_rtlamr(config)
            else:
                rtlamr.poll()
                if rtlamr.returncode is not None:
                    if LOG_LEVEL >= 3:
                        logger.critical('RTLAMR has died, trying to restart...')
                    if int(config['general']['sleep_for']) > 0:
                        if LOG_LEVEL >= 2:
                            logger.info('Sleep for is set to %d seconds...', int(config['general']['sleep_for']))
                        sleep(int(config['general']['sleep_for']))
                    rtlamr = start_rtlamr(config)
                    if rtlamr is not None:
                        rtlamr.poll()

            if rtlamr is None:
                if LOG_LEVEL >= 3:
                    logger.critical('Failed to start RTLAMR. Exiting...')
                shutdown(
                            rtlamr=rtlamr,
                            rtltcp=rtltcp,
                            mqtt_client=mqtt_client,
                            base_topic=config['mqtt']['base_topic'],
                            offline=True
                        )
                sys.exit(1)

            try:
                rtlamr_output = rtlamr.stdout.readline().strip()
                # rtlamr_output = rtlamr.stdout.read1().strip()
            except KeyboardInterrupt:
                logger.critical('Interrupted by user.')
                keep_reading = False
                break
            except Exception as e:
                logger.critical(e)
                keep_reading = False
                break

            if rtlamr_output:
                logger.debug('Received rtlamr message: %s', rtlamr_output)
                reading = ro.get_message(rtlamr_output)

                if reading:
                    logger.debug('Received reading: %s', reading)
                    publish_discovery_if_new(reading['meter_id'])

            # Search for ID in the output
            reading = ro.get_message_for_ids(
                rtlamr_output = rtlamr_output,
                meter_ids_list = meter_ids_list
            )

            if reading is not None:
                # Add the meter_id to the read_counter
                if reading['meter_id'] not in read_counter:
                    read_counter.append(reading['meter_id'])

                consumption = reading['consumption']
                if (decimals := config.get('meters', {}).get(reading.get('meter_id'), {}).get('decimals')):
                    consumption = ro.format_number_with_decimals(consumption, decimals)
                elif (meter_format := config.get('meters', {}).get(reading.get('meter_id'), {}).get('format')):
                    consumption = ro.format_number(consumption, meter_format)

                # Publish the reading to MQTT
                # First, make sure the status is set to online
                mqtt_client.publish(
                    topic=f'{config["mqtt"]["base_topic"]}/status',
                    payload='online',
                    qos=1,
                    retain=False
                )
                # Then, send the reading
                payload = { 'reading': consumption, 'lastseen': get_iso8601_timestamp() }
                mqtt_client.publish(
                    topic=f'{config["mqtt"]["base_topic"]}/{reading["meter_id"]}/state',
                    payload=dumps(payload),
                    qos=1,
                    retain=False
                )

                # Publish the meter attributes to MQTT
                # # Add the meter protocol to the list of attributes
                # reading['message']['protocol'] = config['meters'][reading['meter_id']]['protocol']
                mqtt_client.publish(
                    topic=f'{config["mqtt"]["base_topic"]}/{reading["meter_id"]}/attributes',
                    payload=dumps(reading['message']),
                    qos=1,
                    retain=False
                )

            if config['general']['sleep_for'] > 0 and len(read_counter) == len(meter_ids_list):
                # We have our readings, so we can sleep
                if LOG_LEVEL >= 2:
                    logger.info('All readings received.')
                    logger.info('Sleeping for %d seconds...', config["general"]["sleep_for"])
                # Shutdown everything, but mqtt_client
                shutdown(rtlamr=rtlamr, rtltcp=rtltcp, mqtt_client=None)
                read_counter = []
                try:
                    sleep(int(config['general']['sleep_for']))
                except KeyboardInterrupt:
                    logger.critical('Interrupted by user.')
                    keep_reading = False
                    shutdown(
                        rtlamr=rtlamr,
                        rtltcp=rtltcp,
                        mqtt_client=mqtt_client,
                        base_topic=config['mqtt']['base_topic'],
                        offline=True
                    )
                    break
                except Exception:
                    logger.critical('Term siganal received. Exiting...')
                    keep_reading = False
                    shutdown(
                        rtlamr=rtlamr,
                        rtltcp=rtltcp,
                        mqtt_client=mqtt_client,
                        base_topic=config['mqtt']['base_topic'],
                        offline=True
                    )
                    break
                if LOG_LEVEL >= 3:
                    logger.info('Time to wake up!')

            sleep(1)  # Sleep for a short time to avoid busy waiting
        except RuntimeError as e:
            # Handle the signal received
            logger.critical('Runtime error: %s', e)
            keep_reading = False

    # Shutdown
    shutdown(
        rtlamr = rtlamr,
        rtltcp = rtltcp,
        mqtt_client = mqtt_client,
        base_topic=config['mqtt']['base_topic'],
        offline=True
    )


if __name__ == '__main__':
    # Call main function
    main()
