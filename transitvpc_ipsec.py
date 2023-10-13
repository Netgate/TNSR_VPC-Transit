#!/usr/bin/env python

import urllib.parse
import boto3
import requests
from botocore.client import Config
from xml.dom import minidom


rest_url = 'https://localhost/restconf/data'
headers = {'Content-Type': 'application/yang-data+json'}
cert_file = '/etc/pki/tls/tnsr/certs/restconf-client.crt'
key_file = '/etc/pki/tls/tnsr/private/restconf-client.key'
ca_cert_file = '/etc/pki/tls/tnsr/CA/restconf-CA.crt'

def getTunnelId(vpn_gateway_ip):
  tunnels = requests.get(rest_url+'/netgate-tunnel:tunnels-config/netgate-ipip:ipip', cert=(cert_file, key_file), verify=ca_cert_file)
  if tunnels.status_code != 404 and tunnels.json():
    for tunnel in tunnels.json()['netgate-ipip:ipip']['tunnel']:
      if tunnel['ipv4-remote-endpoint-address'] == vpn_gateway_ip:
        return tunnel['instance']

def getNextTunnelId():
  tunnelId = 0
  listOfTunnels = []
  tunnels = requests.get(rest_url+'/netgate-ipsec:ipsec-config', cert=(cert_file, key_file), verify=ca_cert_file)
  if tunnels.status_code != 404 and tunnels.json():
    for tunnel in  tunnels.json()['netgate-ipsec:ipsec-config']['tunnel']:
      listOfTunnels.append(tunnel['instance'])
    while tunnelId in listOfTunnels:
      tunnelId+=1
    return tunnelId
  return tunnelId

def createIPSec():
  s3 = boto3.client('s3', endpoint_url='https://s3-eu-west-1.amazonaws.com', config=Config(s3={'addressing_style': 'virtual'}, signature_version='s3v4'))

  if 'Contents' in s3.list_objects(Bucket='netgate-transit-vpnconfigs'):
    pass
  else:
    print("AWS S3 Bucket is Empty")
    return

  for file in s3.list_objects(Bucket='netgate-transit-vpnconfigs')['Contents']:
    config = s3.get_object(Bucket='netgate-transit-vpnconfigs', Key=file['Key'])
    xmldoc = minidom.parseString(config['Body'].read())
    vpn_config = xmldoc.getElementsByTagName("transit_vpc_config")[0]
    vpn_status = vpn_config.getElementsByTagName("status")[0].firstChild.data
    spoke_subnet = vpn_config.getElementsByTagName('spoke_subnet')[0].firstChild.data
    vpn_connection = xmldoc.getElementsByTagName('vpn_connection')[0]
    ipsec_tunnel = vpn_connection.getElementsByTagName("ipsec_tunnel")[0]
    customer_gateway = ipsec_tunnel.getElementsByTagName("customer_gateway")[0]
    customer_gateway_ip = customer_gateway.getElementsByTagName("tunnel_outside_address")[0].getElementsByTagName("ip_address")[0].firstChild.data
    customer_inside_ip = customer_gateway.getElementsByTagName("tunnel_inside_address")[0].getElementsByTagName("ip_address")[0].firstChild.data
    customer_local_ip = vpn_config.getElementsByTagName("customer_local_ip")[0].firstChild.data
    vpn_gateway = ipsec_tunnel.getElementsByTagName("vpn_gateway")[0]
    vpn_gateway_ip = vpn_gateway.getElementsByTagName("tunnel_outside_address")[0].getElementsByTagName("ip_address")[0].firstChild.data
    vpn_inside_ip = vpn_gateway.getElementsByTagName("tunnel_inside_address")[0].getElementsByTagName("ip_address")[0].firstChild.data
    ike_config = ipsec_tunnel.getElementsByTagName("ike")[0]
    ike_psk = ike_config.getElementsByTagName("pre_shared_key")[0].firstChild.data

    if vpn_status == 'delete':
      tunnel_id = getTunnelId(vpn_gateway_ip)
      print(tunnel_id)
      print(type(tunnel_id))

      print("Delete IPSec{0} interface from TNSR".format(tunnel_id, vpn_gateway_ip))
      requests.delete(rest_url + '/interfaces-config/interface=ipsec{0}'.format(tunnel_id), params={})
      print("Delete IPSec tunnel{0} with VGW:{1} from TNSR".format(tunnel_id, vpn_gateway_ip))
      ipsecdel = requests.delete(rest_url + '/ipsec-config/tunnel={0}'.format(tunnel_id), params={})
      print("Delete routes to spoke subnet:{0}".format(spoke_subnet))
      requests.delete(rest_url + '/route-table-config/static-routes/route-table=ipv4-VRF:0/ipv4-routes/route='+urllib.parse.quote_plus(spoke_subnet))
      if ipsecdel.status_code == 200:
        print("Delete IPSec config with VGW:{0} from S3".format(vpn_gateway_ip))
        s3.delete_object(Bucket='netgate-transit-vpnconfigs', Key=file['Key'])
        print("IPSec tunnel{0} has deleted".format(tunnel_id))

    elif vpn_status == 'create':
      tunnel_id = getNextTunnelId()
      if not isinstance(getTunnelId(vpn_gateway_ip), int):

        ipsec_config = {
          "netgate-ipsec:tunnel": {
            "instance": 0,
            "tunnel-enable": True,
            "crypto": {
              "config-type":"ike",
              "ike": {
                "key-renewal": "reauth",
                "role": "initiator-only",
                "version":1,
                "lifetime":14400,
                "proposals":[
                  {
                    "name":"1",
                    "encryption-algorithm":"aes128",
                    "integrity-algorithm":"sha1",
                    "dh-group":"modp1024"
                  }
                ],
                "identity":[
                  {
                    "peer":"local",
                    "type":"address",
                    "value":"%VARIABLE%"
                  },
                  {
                    "peer":"remote",
                    "type":"address",
                    "value":"%VARIABLE%"
                  }
                ],
                "authentication":[
                  {
                    "peer":"local",
                    "round":[
                      {
                        "number":1,
                        "psk":"%VARIABLE%"
                      }
                    ]
                  },
                  {
                    "peer":"remote",
                    "round":[
                      {
                        "number":1,
                        "psk":"%VARIABLE%"
                      }
                    ]
                  }
                ],
                "child-sa":[
                  {
                    "name":"1",
                    "lifetime":3600,
                    "proposal":[
                      {
                        "name":"1",
                        "encryption-algorithm":"aes128",
                        "integrity-algorithm":"sha1",
                        "dh-group":"modp1024"
                      }
                    ]
                  }
                ]
              }
            }
          }
        }
        interface_ipsec = {
          "netgate-interface:interface":[
            {
              "name": "%VARIABLE%",
              "enabled":bool('true'),
              "ipv4":{
                "address":{
                  "ip":[
                    "%VARIABLE%"
                  ]
                }
              }
            }
          ]
        }
        route_table_config = {
          "netgate-route-table:route":[
            {
              "destination-prefix":"%VARIABLE%",
              "next-hop":{
                "hop":[
                  {
                    "hop-id":1,
                    "ipv4-address":"%VARIABLE%",
                    "if-name":"%VARIABLE%"
                  }
                ]
              }
            }
          ]
        }
        ipip_config = {
                "netgate-ipip:tunnel": {
                      "instance": 0,
                      "ipv4-local-endpoint-address": "%VARIABLE%",
                      "ipv4-remote-endpoint-address": "%VARIABLE%"
                    }
        }

        ipip = ipip_config['netgate-ipip:tunnel']
        ipip['instance'] = tunnel_id # Tunnel's ID
        ipip['ipv4-local-endpoint-address'] = customer_local_ip # Local PRIP
        ipip['ipv4-remote-endpoint-address'] = vpn_gateway_ip # Remote EXTIP
        tunnel = ipsec_config['netgate-ipsec:tunnel']
        tunnel['instance'] = tunnel_id # Tunnel's ID
        tunnel['crypto']['ike']['authentication'][0]['round'][0]['psk'] = ike_psk # Local PSK KEY
        tunnel['crypto']['ike']['authentication'][1]['round'][0]['psk'] = ike_psk # Remote PSK KEY
        tunnel['crypto']['ike']['identity'][0]['value'] = customer_gateway_ip # Local EXTIP
        tunnel['crypto']['ike']['identity'][1]['value'] = vpn_gateway_ip # Remote EXTIP

        interface_ipsec['netgate-interface:interface'][0]['name'] = "ipsec{0}".format(tunnel_id)
        interface_ipsec['netgate-interface:interface'][0]['ipv4']['address']['ip'][0] = "{0}/30".format(customer_inside_ip)

        route_table_config['netgate-route-table:route'][0]['destination-prefix'] = spoke_subnet
        route_table_config['netgate-route-table:route'][0]['next-hop']['hop'][0]['ipv4-address'] = vpn_inside_ip
        route_table_config['netgate-route-table:route'][0]['next-hop']['hop'][0]['if-name'] = "ipsec{0}".format(tunnel_id)

        print("Configuring ipip{0} tunnel".format(tunnel_id))
        out=requests.put(rest_url+'/netgate-tunnel:tunnels-config/netgate-ipip:ipip/tunnel={0}'.format(tunnel_id), params={}, json=ipip_config, headers=headers, cert=(cert_file, key_file), verify=ca_cert_file)
        print(out.text)
        print("Creating IPSec tunnel{0} with vgw-{1}".format(tunnel_id, vpn_gateway_ip))
        out=requests.put(rest_url+'/netgate-ipsec:ipsec-config/tunnel={0}'.format(tunnel_id), params={}, json=ipsec_config, headers=headers, cert=(cert_file, key_file), verify=ca_cert_file)
        print(out.text)
        print("Configuring ipsec{0} interface".format(tunnel_id))
        out=requests.put(rest_url+'/netgate-interface:interfaces-config/interface=ipsec{0}'.format(tunnel_id), params={}, json=interface_ipsec, headers=headers, cert=(cert_file, key_file), verify=ca_cert_file)
        print(out.text)
        print("Configuring ipsec{0} routes".format(tunnel_id)) 
        out=requests.put(rest_url+'/netgate-route-table:route-table-config/static-routes/route-table=ipv4-VRF:0/ipv4-routes/route='+urllib.parse.quote_plus(spoke_subnet), params={}, json=route_table_config, headers=headers, cert=(cert_file, key_file), verify=ca_cert_file)
        print(out.text)
        print("IPSec tunnel{0} with vpn-getaway:{1} and subnet-{2} has created".format(tunnel_id, vpn_gateway_ip, spoke_subnet))

      else:
        print("Tunnel with vgw {0} already exists, skipping configuration.".format(vpn_gateway_ip))
        pass

if __name__ == "__main__":
  createIPSec()
