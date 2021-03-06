import argparse
import base64
import cv2
import os
import re
import time
import socket
import uuid

from apiclient.discovery import build
from oauth2client.client import GoogleCredentials

# Author: reddyv@
# Last Update: 04-02-2016
# Usage:
#   python vid_moderator.py --help
# ToDo:
#   1) Evaluate using ffmpeg instead of OpenCV. Can you get faster than
#   200ms/frame? This is the current performance bottleneck
#   2) Figure out why application default credentials don't work for vision API
#   3) See if you can convert the cv2 image format to base64 directly in memory 
#   without having to write to disk first. Note this is lower priority because
#   the disk read/write time is still an order of magnitude less than the frame
#   grabbing time (10ms vs 200ms), at least on SSD storage
#   4) Allow passing feature set to analyze (logo,label, explicit, OCR) as parameter
#   5) Replace django uploadFile with standard Python way. Then you can
#   completely eliminate the django framework.

def moderate(video_file, APIKey, sample_rate=5, response_type=1):
  timer_total = time.time()
  BATCH_LIMIT = 35 #number of images to send per API request. Documented limit
  # is 16 images per request but i've tested up to 150 per request with success
  
  #obtain service handle for vision API using API Key
  #Note: would prefer to use application default credentials rather than API key
  # but get the following error when i try. Appears to be trying to authenticate
  # to a different project than the one specified during gcloud init
  """googleapiclient.errors.HttpError: <HttpError 403 when requesting
   https://vision.googleapis.com/v1/images:annotate?alt=json returned "Project 
   has not activated the vision.googleapis.com API. Please enable the API for 
   project google.com:cloudsdktool (#32555940559).">"""

  service = build('vision', 'v1',
  discoveryServiceUrl='https://vision.googleapis.com/$discovery/rest?version=v1',
  developerKey=APIKey)
  
  #initialize vars 
  position = 0
  frame = 0
  batch_count = 0
  base64_images = []
  frame_annotations = ''
  performance_metrics = '.\n' #the '.' is because otherwise the newline is ignored by locust
  unique_file_name = uuid.uuid4()
  temp_mp4 = str(unique_file_name) + '.mp4'
  temp_jpg = str(unique_file_name) + '.jpg'
  vidcap = ''

  if isinstance(video_file, unicode): #download file from gcs
    #get application default credentials (specified during gcloud init)
    credentials = GoogleCredentials.get_application_default()

    #construct service handle
    gcs_service = build('storage', 'v1', credentials=credentials)

    #extract GCS bucket and object from file name
    re_match = re.match(r'gs://(.*?)/(.*)', video_file, re.I)
    
    #'get_media' returns file contents while 'get' returns file metadata
    req = gcs_service.objects().get_media(bucket=re_match.group(1), 
      object=re_match.group(2))

    #execute request and save response to disk
    timer_gcs = time.time()
    with open(temp_mp4,'w') as file:
      file.write(req.execute())
    performance_metrics += 'Fetching GCS File: ' + str(
      int((time.time() - timer_gcs) * 1000)) + 'ms <br><br>\n\n'

  else: #file is local file
    vidcap = cv2.VideoCapture(video_file)

  #grab first frame
  #format note: this has been tested with the mp4 video format ONLY     
  if (not(vidcap)): vidcap = cv2.VideoCapture(temp_mp4)
  success,image = vidcap.read()
  
  while success: 
    
    #read in frames one batch at a time
    timer_batch_total = time.time()
    timer_batch_frame_grabbing = time.time()
    while success and batch_count < BATCH_LIMIT:

      #convert frame to base64
      cv2.imwrite(temp_jpg, image)
      with open(temp_jpg,'rb') as image:
        base64_images.append((position/1000,base64.b64encode(image.read())))


      #advance to next image
      if sample_rate > 0: position = position+1000*sample_rate
      else: position = -1 #terminate
      frame += 1
      batch_count += 1
      vidcap.set(0,position)

      #vicap.read() takes ~200ms per frame on 2.2 GHz Intel Core i7
      #this is the performance bottleneck
      success,image = vidcap.read()
    performance_metrics += 'Frame Grabbing: '+str(int((time.time() - timer_batch_frame_grabbing) * 1000))+'ms <br>\n'
    #send batch to vision API
    json_request = {'requests': []}
    for img in base64_images:
      json_request['requests'].append(
        {
          'image': {
            'content': img[1] #recall img is a tuple (timestamp, base64image)
           },
          'features': [
           {
            'type': 'SAFE_SEARCH_DETECTION',
           },
           {
            'type': 'LABEL_DETECTION',
            'maxResults': 3,
           },
           {
            'type': 'LOGO_DETECTION',
            'maxResults': 3,
           },
           {
            'type': 'TEXT_DETECTION',
            'maxResults': 3,
           }
           ]
        })
    service_request = service.images().annotate(body=json_request)

    #API performance
    # tl;dr: the more you batch the better
    #  1 frame batch takes ~1 sec
    #  10 frame batch takes ~1.5 sec
    #  100 frame batch takes ~4.0 sec
    # Note these numbers should drop a bit when running the app from the cloud
    # due to reduced latency. But relative differences should hold.
    timer_batch_api = time.time()
    responses = service_request.execute()
    performance_metrics += 'API request: ' + str(int((time.time() - timer_batch_api) * 1000)) + 'ms <br>\n'
    #response format
    #{u'responses': [{u'labelAnnotations': [{u'score': 0.99651724, u'mid':
    # u'/m/01c4rd', u'description': u'beak'}, {u'score': 0.96588981, u'mid':
    # u'/m/015p6', u'description': u'bird'}, {u'score': 0.85704041, u'mid':
    # u'/m/09686', u'description': u'vertebrate'}]}]}

    #process response and print results
    if responses.has_key('responses'):
      for response, img in zip(responses.get('responses'),base64_images):

        #print frame timestamp
        frame_annotations += ('<h3>'+str(img[0])+'sec</h3>')

        #process labels
        frame_annotations += ('\tLabels:')
        if response.has_key('labelAnnotations'):
          frame_annotations += printEntityAnnotation(response.get('labelAnnotations'))
        else: frame_annotations += ('no labels identified<br>')

        #process logos
        frame_annotations += ('\tLogos:')
        if response.has_key('logoAnnotations'):
          frame_annotations += printEntityAnnotation(response.get('logoAnnotations'))
        else: frame_annotations += ('no logos identified<br>')

        #process safe search
        frame_annotations += ('\tSafe Search:<br>')
        if response.has_key('safeSearchAnnotation'):
          frame_annotations += ('\t  Adult Content is '+
            response.get('safeSearchAnnotation').get('adult') + '<br>')
          frame_annotations += ('\t  Violent Content is '+
            response.get('safeSearchAnnotation').get('violence') + '<br>')
        else: frame_annotations += ('\t\tno safe search results<br>')

        #process text (OCR-optical character recognition)
        frame_annotations += ('\tText:')
        if response.has_key('textAnnotations'):
          frame_annotations += printEntityAnnotation(response.get('textAnnotations'))
        else: frame_annotations += ('no text identified<br>')

    else: frame_annotations += ('no response<br>')
    performance_metrics += 'Batch Total (' + str(batch_count) + ' frames): ' + \
                                          str(int((time.time() - timer_batch_total) * 1000)) + 'ms <br><br>\n\n'

    #reset for next batch
    batch_count = 0
    base64_images = []

  
  #cleanup
  os.remove(temp_jpg)
  if os.path.isfile(temp_mp4): os.remove(temp_mp4)


  if response_type == 1: return performance_metrics + \
                                'Total: ' + str(int((time.time() - timer_total) * 1000)) + 'ms <br>\n' + \
                                'Pod: ' + socket.gethostname() + '<br>\n.'
  else: return frame_annotations

def printEntityAnnotation(annotations):
  entities = ''
  for annotation in annotations:
    entities += annotation['description']+', '
  entities = entities[:-2] #trim trailing comma and space
  return entities + '<br>' # may need to add .encode(utf-8)


if __name__ == '__main__':
  
  #configure command line options
  parser = argparse.ArgumentParser(
    description='Feed a video to the Google Vision API')
  parser.add_argument(
    'file_name', help=('The video you\'d like to process. Can either pass a '
    'local file or a GCS file in the format "gs://<bucket-name>/<file'
    '-path>"'))
  parser.add_argument(
    'APIKey', help=('The API Key that identifies your Google Cloud Console '
    'Project with Vision API Enabled'))
  parser.add_argument(
    '-s','--sample-rate',dest='samplerate', default=5, type=int, 
    help=('The rate at which stills should be sampled from the '
    'video. Default is 5 (one frame per 5 seconds).'))
  parser.add_argument(
    '-r','--response-type',dest='responsetype', default=1, type=int,
    help=('1 for performance metrics, 2 for frame annotations. Default is 1.')
  )
  
  #read in command line arguments
  args = parser.parse_args()
  
  #start execution
  print(moderate(args.file_name, args.APIKey, args.samplerate, args.responsetype).encode('utf-8'))
