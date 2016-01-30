#!/usr/bin/env python

import requests
import re
import sys
import argparse
import os
import logging
import datetime
import time
import csv
import tempfile
from util import settings
from util import util
from xml.etree import ElementTree
from collections import defaultdict
import json

# Fake class only for purpose of limiting global namespace to the 'g' object
class g:
    args = None


def main(argv):

    global g

    parser = argparse.ArgumentParser()
    parser.add_argument('--input-events-filename', required=False, help='Name of input CSV file from previous ' +
        'event occurrences retrieval. If not specified, event list CSV data is retrieved from CCB UI.')
    parser.add_argument('--output-events-filename', required=False, help='Name of CSV output file listing event ' +
        'information. Defaults to ./tmp/events_[datetime_stamp].csv')
    parser.add_argument('--output-attendance-filename', required=False, help='Name of CSV output file listing ' +
        'attendance information. Defaults to ./tmp/attendance_[datetime_stamp].csv')
    parser.add_argument('--message-level', required=False, help="Either 'Info', 'Warning', or 'Error'. " +
        "Defaults to 'Warning' if unspecified. Log outputs greater or equal to specified severity are emitted " +
        "to message output.")
    parser.add_argument('--message-output-filename', required=False, help='Filename of message output file. If ' +
        'unspecified, defaults to stderr')
    parser.add_argument('--keep-temp-file', action='store_true', help='If specified, temp event occurrences CSV ' + \
        'file created with CSV data pulled from CCB UI (event list report) is not deleted so it can be used ' + \
        'in subsequent runs')
    g.args = parser.parse_args()

    util.set_logger(g.args.message_level, g.args.message_output_filename, os.path.basename(__file__))

    curr_date_str = datetime.datetime.now().strftime('%m/%d/%Y')

    login_request = {
        'ax': 'login',
        'rurl': '/index.php',
        'form[login]': settings.login_info.ccb_app_username,
        'form[password]': settings.login_info.ccb_app_password
    }

    event_list_info = {
        "id":"",
        "type":"event_list",
        "date_range":"",
        "ignore_static_range":"static",
        "start_date":"01/01/2000",
        "end_date":curr_date_str,
        "additional_event_types":["","non_church_wide_events","filter_off"],
        "campus_ids":["1"],
        "output":"csv"
    }

    event_list_request = {
        'request': json.dumps(event_list_info),
        'output': 'export'
    }

    # Set events and attendance filenames and test validity
    if g.args.output_events_filename is not None:
        output_events_filename = g.args.output_events_filename
    else:
        output_events_filename = './tmp/events_' + datetime.datetime.now().strftime('%Y%m%d%H%M%S') + '.csv'
    util.test_write(output_events_filename)
    if g.args.output_attendance_filename is not None:
        output_attendance_filename = g.args.output_attendance_filename
    else:
        output_attendance_filename = './tmp/attendance_' + \
            datetime.datetime.now().strftime('%Y%m%d%H%M%S') + '.csv'
    util.test_write(output_attendance_filename)

    # Pull event profiles XML from CCB REST API (and stash in local temp file to use as input)
    logging.info('Retrieving event profiles from CCB REST API')
    response = requests.get('https://ingomar.ccbchurch.com/api.php?srv=event_profiles', stream=True,
        auth=(settings.login_info.ccb_api_username, settings.login_info.ccb_api_password))
    if response.status_code == 200:
        response.raw.decode_content = True
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            input_filename = temp.name
            for chunk in response.iter_content(chunk_size=1024):
                if chunk: # filter out keep-alive new chunks
                    temp.write(chunk)
            temp.flush()
    else:
        logging.error('CCB REST API call for event_profiles failed. Aborting!')
        sys.exit(1)

    # Properties to peel off each 'event' node in XML
    list_event_props = [
        'name',
        'description',
        'leader_notes',
        'start_datetime',
        'end_datetime',
        'timezone',
        'recurrence_description',
        'approval_status',
        'listed',
        'public_calendar_listed'
    ] # Also collect event_id, group_id, organizer_id

    path = []
    dict_list_event_names = defaultdict(list)
    with open(output_events_filename, 'wb') as csv_output_events_file:
        csv_writer_events = csv.writer(csv_output_events_file)
        csv_writer_events.writerow(['event_id'] + list_event_props + ['group_id', 'organizer_id']) # Write header row
        for event, elem in ElementTree.iterparse(input_filename, events=('start', 'end')):
            if event == 'start':
                path.append(elem.tag)
                full_path = '/'.join(path)
                if full_path == 'ccb_api/response/events/event':
                    current_event_id = elem.attrib['id']
            elif event == 'end':
                if full_path == 'ccb_api/response/events/event':
                    # Emit 'events' row
                    props_csv = get_elem_id_and_props(elem, list_event_props)
                    event_id = props_csv[0] # get_elem_id_and_props() puts 'id' prop at index 0
                    name = props_csv[1] # Cheating here...we know 'name' prop is index 1
                    dict_list_event_names[name].append(event_id)
                    props_csv.append(current_group_id)
                    props_csv.append(current_organizer_id)
                    csv_writer_events.writerow(props_csv)
                    elem.clear() # Throw away 'event' node from memory when done processing it
                elif full_path == 'ccb_api/response/events/event/group':
                    current_group_id = elem.attrib['id']
                elif full_path == 'ccb_api/response/events/event/organizer':
                    current_organizer_id = elem.attrib['id']
                path.pop()
                full_path = '/'.join(path)

    if g.args.input_events_filename is not None:
        # Pull calendared events CSV from file
        input_filename = g.args.input_events_filename
    else:
        # Create UI user session to pull list of calendared events
        logging.info('Logging in to UI session')
        with requests.Session() as http_session:
            # Login
            login_response = http_session.post('https://ingomar.ccbchurch.com/login.php', data=login_request)
            login_succeeded = False
            if login_response.status_code == 200:
                match_login_info = re.search('individual: {\s+id: 5,\s+name: "' + \
                    settings.login_info.ccb_app_login_name + '"', login_response.text)
                if match_login_info != None:
                    login_succeeded = True
            if not login_succeeded:
                logging.error('Login to CCB app using username ' + settings.login_info.ccb_app_username +
                    ' failed. Aborting!')
                sys.exit(1)

            # Get list of all scheduled events
            logging.info('Retrieving list of all scheduled events.  This might take a couple minutes!')
            event_list_response = http_session.post('https://ingomar.ccbchurch.com/report.php',
                data=event_list_request)
            event_list_succeeded = False
            if event_list_response.status_code == 200:
                event_list_response.raw.decode_content = True
                with tempfile.NamedTemporaryFile(delete=False) as temp:
                    input_filename = temp.name
                    first_chunk = True
                    for chunk in event_list_response.iter_content(chunk_size=1024):
                        if chunk: # filter out keep-alive new chunks
                            if first_chunk:
                                if chunk[:13] != '"Event Name",':
                                    logging.error('Mis-formed calendared events CSV returned. Aborting!')
                                    sys.exit(1)
                                first_chunk = False
                            temp.write(chunk)
                    temp.flush()

    with open(input_filename, 'rb') as csvfile:
        csv_reader = csv.reader(csvfile)
        with open(output_attendance_filename, 'wb') as csv_output_file:
            csv_writer = csv.writer(csv_output_file)
            csv_writer.writerow(['event_id', 'event_occurrence', 'individual_id', 'count'])
            header_row = True
            for row in csv_reader:
                if header_row:
                    header_row = False
                    output_csv_header = row
                    event_name_column_index = row.index('Event Name')
                    attendance_column_index = row.index('Actual Attendance')
                    date_column_index = row.index('Date')
                    start_time_column_index = row.index('Start Time')
                else:
                    # Retrieve attendees for events which have non-zero number of attendees
                    if row[attendance_column_index] != '0':
                        if row[event_name_column_index] in dict_list_event_names:
                            retrieve_attendance(csv_writer, dict_list_event_names[row[event_name_column_index]],
                                row[date_column_index], row[start_time_column_index],
                                row[attendance_column_index])
                        else:
                            logging.warning("Unrecognized event name '" + row[event_name_column_index] + "'")

    # If caller didn't specify input filename, then delete the temporary file we retrieved into
    if g.args.input_events_filename is None:
        if g.args.keep_temp_file:
            logging.info('Temporary downloaded calendared events CSV retained in file: ' + input_filename)
        else:
            os.remove(input_filename)

    logging.info('Event profile data written to ' + output_events_filename)
    logging.info('Attendance data written to ' + output_attendance_filename)


def get_elem_id_and_props(elem, list_props):
    output_list = [ elem.attrib['id'] ]
    for prop in list_props:
        sub_elem = elem.find(prop)
        if sub_elem is None or sub_elem.text is None:
            output_list.append('')
        else:
            output_list.append(sub_elem.text.encode('ascii', 'ignore'))
    return output_list


def retrieve_attendance(csv_writer, event_id_list, date, start_time, attendance):
    for event_id in reversed(event_id_list):
        time_object = time.strptime(str(date) + ' ' + str(start_time), '%Y-%m-%d %I:%M %p')
        occurrence_datetime_string = time.strftime('%Y-%m-%d+%H:%M:00', time_object)
        ccb_rest_service_string = 'attendance_profile&id=' + str(event_id) + '&occurrence=' + \
            occurrence_datetime_string
        xml_temp_file = ccb_rest_xml_to_temp_file(ccb_rest_service_string)
        if xml_temp_file is not None:
            valid_attendance_data = process_attendance_xml_file(csv_writer, xml_temp_file, event_id,
                occurrence_datetime_string)
            os.remove(xml_temp_file)
            if valid_attendance_data:
                break


def process_attendance_xml_file(csv_writer, input_xml_filename, event_id, event_occurrence_datetime):
    xml_tree = ElementTree.parse(input_xml_filename)
    xml_root = xml_tree.getroot()
    for message in xml_root.findall('./messages/message'):
        logging.info(message.text)
        return False
    for attendee in xml_root.findall('./response/events/event/attendees/attendee'):
        csv_writer.writerow([event_id, event_occurrence_datetime, attendee.attrib['id'], 1])
    for headcount in xml_root.findall('./response/events/event/head_count'):
        headcount_number = int(headcount.text)
        if headcount_number > 0:
            csv_writer.writerow([event_id, event_occurrence_datetime, '', headcount_number])
            break
    return True


def ccb_rest_xml_to_temp_file(ccb_rest_service_string):
    logging.info('Retrieving ' + ccb_rest_service_string + ' from CCB REST API')
    response = requests.get('https://ingomar.ccbchurch.com/api.php?srv=' + ccb_rest_service_string, stream=True,
        auth=(settings.login_info.ccb_api_username, settings.login_info.ccb_api_password))
    if response.status_code == 200:
        response.raw.decode_content = True
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            input_filename = temp.name
            for chunk in response.iter_content(chunk_size=1024):
                if chunk: # filter out keep-alive new chunks
                    temp.write(chunk)
            temp.flush()
        return input_filename
    else:
        logging.warning('CCB REST API call retrieval for ' + ccb_rest_service_string + ' failed')
        return None


if __name__ == "__main__":
    main(sys.argv[1:])
