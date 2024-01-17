######################################################################################################################
#  Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.                                           #
#                                                                                                                    #
#  Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance        #
#  with the License. A copy of the License is located at                                                             #
#                                                                                                                    #
#      http://aws.amazon.com/asl/                                                                                    #
#                                                                                                                    #
#  or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES #
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions    #
#  and limitations under the License.                                                                                #
######################################################################################################################

import boto3
from botocore.client import Config
from xml.dom import minidom
import os
import logging
import datetime 
import sys
import json
import urllib.request
import urllib.parse as urlparse
import re

log_level = str(os.environ.get('LOG_LEVEL')).upper()
if log_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
    log_level = 'ERROR'
log = logging.getLogger()
log.setLevel(log_level)

bucket_name = str(os.environ.get('BUCKET_NAME'))
bucket_prefix = str(os.environ.get('BUCKET_PREFIX'))
config_file = str(os.environ.get('CONFIG_FILE'))
EIP = str(os.environ.get('EIP'))
PIP = str(os.environ.get('PIP'))
HUB_TAG = str(os.environ.get('HUB_TAG'))
HUB_TAG_VALUE = str(os.environ.get('HUB_TAG_VALUE'))
BGP_ASN = int(os.environ.get('BGP_ASN'))
UUID = ''
LOG_LEVEL = 'INFO'


def getTags(vgwTags):
    tags = {}
    for subTag in vgwTags:
        tags[subTag['Key']] = subTag['Value']
    return tags

# This function adds a <transit_vpc_config /> block to an existing XML doc and returns the new XML
def updateConfigXML(xml, vgwTags, account_id, spoke_subnet, csr_number):
    xmldoc = minidom.parseString(xml)
    # Create TransitVPC config xml block
    transitConfig = xmldoc.createElement("transit_vpc_config")
    # Create Account ID xml block
    newXml = xmldoc.createElement("account_id")
    newXml.appendChild(xmldoc.createTextNode(account_id))
    transitConfig.appendChild(newXml)

    # Create VPN Endpoint xml block
    newXml = xmldoc.createElement("vpn_endpoint")
    newXml.appendChild(xmldoc.createTextNode(csr_number))
    transitConfig.appendChild(newXml)

    # Create VPN Spoke subnet xml block
    newXml = xmldoc.createElement("spoke_subnet")
    newXml.appendChild(xmldoc.createTextNode(spoke_subnet))
    transitConfig.appendChild(newXml)

    # Create Transit IPSec local INT xml block
    newXml = xmldoc.createElement("customer_local_ip")
    newXml.appendChild(xmldoc.createTextNode(PIP))
    transitConfig.appendChild(newXml)

    # Create status xml block (create = tagged to create spoke, delete = tagged as spoke, but not with the correct spoke tag value)
    newXml = xmldoc.createElement("status")
    if vgwTags[HUB_TAG] == HUB_TAG_VALUE:
        newXml.appendChild(xmldoc.createTextNode("create"))
    else:
        newXml.appendChild(xmldoc.createTextNode("delete"))
    transitConfig.appendChild(newXml)

    # Add transit config to XML
    xmldoc.childNodes[0].appendChild(transitConfig)
    return str(xmldoc.toxml())


def lambda_handler(event, context):
    # Figure out the account number by parsing this function's ARN
    account_id = re.findall(':(\d+):', context.invoked_function_arn)[0]
    # Retrieve Transit VPC configuration from transit_vpn_config.txt
    s3 = boto3.client('s3', config=Config(signature_version='s3v4'))
    log.info('Getting config file %s/%s%s', bucket_name, bucket_prefix)

    log.info('Retrieved IP of transit VPN gateways: %s, %s', EIP)
    # Use this variable to determine if a VGW has been processed so we will only process one VGW per run (one per minute)
    processed_vgw = False
    # Get list of regions so poller can look for VGWs in all regions
    ec2 = boto3.client('ec2', region_name='us-east-1')
    regions = ec2.describe_regions()    
    for region in regions['Regions']:
        # Get region name for the current region
        region_id = region['RegionName']
        log.debug('Checking region: %s', region_id)
        # Create EC2 connection to this region to get list of VGWs
        ec2 = boto3.client('ec2', region_name=region_id)
        # Get list of all VGWs in the region
        vgws = ec2.describe_vpn_gateways(Filters=[
            {'Name': 'state', 'Values': ['available', 'attached', 'detached']}
        ])
        # Get list of Transit VPC tagged VPN connections in the region as well
        vpns = ec2.describe_vpn_connections(Filters=[
            {'Name': 'state', 'Values': ['available', 'pending', 'deleting']},
            {'Name': 'tag:' + HUB_TAG, 'Values': [HUB_TAG_VALUE]}
        ])

        # Process all the VGWs in the region
        for vgw in vgws['VpnGateways']:
            # Check to see if the VGW has tags, if not, then we should skip it
            if vgw.get('Tags', '') == '':
                continue

            # Put all of the VGW tags into a dict for easier processing
            vgwTags = getTags(vgw['Tags'])
            # Configure HUB_TAG if it is not set already (for untagged VGWs)
            vgwTags[HUB_TAG] = vgwTags.get(HUB_TAG, '')

            # Determine if VGW is tagged as a spoke
            spoke_vgw = False
            if vgwTags[HUB_TAG] == HUB_TAG_VALUE:
              spoke_vgw = True

            # Check to see if the VGW already has Transit VPC VPN Connections
            vpn_existing = False
            for vpn in vpns['VpnConnections']:
              if vpn['VpnGatewayId'] == vgw['VpnGatewayId']:
                  vpn_existing = True
                  break

            # Need to create VPN connections if this is a spoke VGW and no VPN connections already exist
            if spoke_vgw and not vpn_existing:
                log.info('Found a new VGW (%s) which needs VPN connections.', vgw['VpnGatewayId'])
                # Create Customer Gateways (will create CGWs if they do not exist, otherwise, the API calls are ignored)
                log.debug('Creating Customer Gateways with IP %s, %s', EIP)
                cg1 = ec2.create_customer_gateway(Type='ipsec.1', PublicIp=EIP, BgpAsn=BGP_ASN)
                ec2.create_tags(Resources=[cg1['CustomerGateway']['CustomerGatewayId']],
                                Tags=[{'Key': 'Name', 'Value': 'Transit VPC Endpoint1'}])
                log.info('Created Customer Gateways: %s, %s', cg1['CustomerGateway']['CustomerGatewayId'])

                # Create and tag first VPN connection
                vpn1 = ec2.create_vpn_connection(Type='ipsec.1', CustomerGatewayId=cg1['CustomerGateway']['CustomerGatewayId'],
                                                VpnGatewayId=vgw['VpnGatewayId'], Options={'StaticRoutesOnly': True})

                ec2.create_tags(Resources=[vpn1['VpnConnection']['VpnConnectionId']],
                                Tags=[
                                    {'Key': 'Name', 'Value': vgw['VpnGatewayId'] + '-to-Transit-VPC CSR1'},
                                    {'Key': HUB_TAG, 'Value': HUB_TAG_VALUE},
                                    {'Key': 'transitvpc:endpoint', 'Value': 'CSR1'}
                                ])
                log.info('Created VPN connections: %s, %s', vpn1['VpnConnection']['VpnConnectionId'])

                # Retrieve VPN configuration
                vpn_config1 = ec2.describe_vpn_connections(VpnConnectionIds=[vpn1['VpnConnection']['VpnConnectionId']])
                vpn_config1 = vpn_config1['VpnConnections'][0]['CustomerGatewayConfiguration']
                # Spoke subnet
                spoke_subnet = ec2.describe_vpcs(VpcIds=[vgw['VpcAttachments'][0]['VpcId']])['Vpcs'][0]['CidrBlock']
                # Update VPN configuration XML with transit VPC specific configuration info for this connection
                vpn_config1 = updateConfigXML(vpn_config1, vgwTags, account_id, spoke_subnet, 'CSR1')
                # Put CSR1 config in S3
                s3.put_object(
                    Body=str.encode(vpn_config1),
                    Bucket=bucket_name,
                    Key=bucket_prefix + 'CSR1/' + region_id + '-' + vpn1['VpnConnection']['VpnConnectionId'] + '.conf',
                    ACL='bucket-owner-full-control',
                )
                log.debug('Pushed VPN configurations to S3...')
                processed_vgw = True

            # Need to delete VPN connections if this is no longer a spoke VPC (tagged for spoke, but tag != spoke tag value) but Transit VPC connections exist
            if not spoke_vgw and vpn_existing:
                log.info('Found old VGW (%s) with VPN connections to remove.', vgw['VpnGatewayId'])
                # We need to go through the region's VPN connections to find the ones to delete
                for vpn in vpns['VpnConnections']:
                    if vpn['VpnGatewayId'] == vgw['VpnGatewayId']:
                        # Put the VPN tags into a dict for easier processing
                        vpnTags = getTags(vpn['Tags'])
                        if vpnTags['transitvpc:endpoint'] == 'CSR1':
                            csrNum = '1'
                        else:
                            csrNum = '2'
                        # Need to get VPN configuration to remove from CSR
                        vpn_config = vpn['CustomerGatewayConfiguration']
                        # Spoke subnet
                        spoke_subnet = ec2.describe_vpcs(
                            VpcIds=[vgw['VpcAttachments'][0]['VpcId']])['Vpcs'][0]['CidrBlock']
                        # Update VPN configuration XML with transit VPC specific configuration info for this connection
                        vpn_config = updateConfigXML(vpn_config, vgwTags, account_id, spoke_subnet,
                                                     vpnTags['transitvpc:endpoint'])

                        s3.put_object(
                            Body=str.encode(vpn_config),
                            Bucket=bucket_name,
                            Key=bucket_prefix + 'CSR' + csrNum + '/' + region_id + '-' + vpn['VpnConnectionId'] + '.conf',
                            ACL='bucket-owner-full-control',
                        )
                        log.debug('Pushed CSR%s configuration to S3.', csrNum)
                        # Now we need to delete the VPN connection
                        ec2.delete_vpn_connection(VpnConnectionId=vpn['VpnConnectionId'])
                        log.info('Deleted VPN connection (%s) to CSR%s', vpn['VpnConnectionId'], csrNum)
                        # Attempt to clean up the CGW. This will only succeed if the CGW has no VPN connections are deleted
                        try:
                            ec2.delete_customer_gateway(CustomerGatewayId=vpn['CustomerGatewayId'])
                            log.info("Cleaned up %s since it has no VPN connections left", vpn['CustomerGatewayId'])
                        except:
                            log.debug("%s still has existing VPN connections", vpn['CustomerGatewayId'])

            # If a VGW has been processed, then we need to break out of VGW processing
            if processed_vgw:
                break

        # If a VGW has been processed, then we need to break out of region processing
        if processed_vgw:
            break
