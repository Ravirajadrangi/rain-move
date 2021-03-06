#!/usr/bin/env python
# -------------------------------------------------------------------------- #
# Copyright 2010-2011, Indiana University                                    #
#                                                                            #
# Licensed under the Apache License, Version 2.0 (the "License"); you may    #
# not use this file except in compliance with the License. You may obtain    #
# a copy of the License at                                                   #
#                                                                            #
# http://www.apache.org/licenses/LICENSE-2.0                                 #
#                                                                            #
# Unless required by applicable law or agreed to in writing, software        #
# distributed under the License is distributed on an "AS IS" BASIS,          #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.   #
# See the License for the specific language governing permissions and        #
# limitations under the License.                                             #
# -------------------------------------------------------------------------- #


__author__ = 'Javier Diaz'

import argparse
import logging
import logging.handlers
import os
import re
import socket
import ssl
import sys
import time
#from multiprocessing import Process, Queue
from threading import Thread, Lock

from futuregrid_move.rain.move.Resource import Resource, Node, Cluster, Service
from futuregrid_move.rain.move.HPCService import HPCService
from futuregrid_move.rain.move.EucaService import EucaService
from futuregrid_move.rain.move.OpenStackService import OpenStackService
from futuregrid_move.rain.move.OpenNebulaService import OpenNebulaService
from futuregrid_move.rain.move.NimbusService import NimbusService
from futuregrid_move.rain.move.Fabric import Fabric, Inventory, InventoryMongoDB, InventoryFile, InventoryDB

from futuregrid_move.rain.move.RainMoveServerConf import RainMoveServerConf
from futuregrid_move.utils import FGAuth
from futuregrid_move.utils.FGTypes import FGCredential

class RainMoveServer(object):

    def __init__(self, inventoryFile):
        super(RainMoveServer, self).__init__()
        
        self.numparams = 7   
        
        self.user = ''
        self.element = ''
        self.operation = ''
        self.arguments = None
        self.forcemove = False
        
        
        #load from config file
        self._rainConf = RainMoveServerConf()
        self._rainConf.load_moveServerConfig() 
                
        self.port = self._rainConf.getMovePort()
        self.authorizedusers = self._rainConf.getMoveAuthorizedUsers()
        self.log_filename = self._rainConf.getMoveLog()
        self.logLevel = self._rainConf.getMoveLogLevel()
        
        self.protectedMachines=self._rainConf.getMovesProtectedMachines()
        
        self.proc_max = self._rainConf.getMoveProcMax()
        self.refresh_status = self._rainConf.getMoveRefreshStatus()
        
        self._ca_certs = self._rainConf.getMoveServerCaCerts()
        self._certfile = self._rainConf.getMoveServerCertFile()
        self._keyfile = self._rainConf.getMoveServerKeyFile()
        
        self.dbaddress = self._rainConf.getMoveDbAddress() 
        self.dbport = self._rainConf.getMoveDbPort()
        self.dbname = self._rainConf.getMoveDbName()
        
        print "\nReading Configuration file from " + self._rainConf.getConfigFile() + "\n"
        
        self.logger = self.setup_logger()
        
        self.fgfabric = Fabric(self._rainConf, self.logger, False)  #Fabric object
        
        if inventoryFile != None:
            fginventoryFile = InventoryFile(inventoryFile)
            nodesFile, servicesFile = self.fgfabric.loadGetNodesServices(fginventoryFile)

        fginventory = InventoryMongoDB(self.dbaddress, self.dbport, self.dbname)
        if fginventory.mongoConnection():
            if inventoryFile != None: #if we have this file we write it on DB
                fginventory.fromMemory2DB(nodesFile, servicesFile)
            self.fgfabric.load(fginventory)
        else:
            print "ERROR: connection with the database failed"
            sys.exit(-1)
        
        """
        if inventoryfile != None:
            fginventory = InventoryFile(inventoryfile)
            self.fgfabric.load(fginventory)
        """
    """       
    def load(self, inventoryfile):
        fginventory = InventoryFile(inventoryfile)
        self.fgfabric.load(fginventory)
    """
    def setup_logger(self):
        #Setup logging
        logger = logging.getLogger("RainMoveServer")
        logger.setLevel(self.logLevel)    
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler = logging.FileHandler(self.log_filename)
        handler.setLevel(self.logLevel)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False #Do not propagate to others
        
        return logger
    
    def auth(self, userCred):
        return FGAuth.auth(self.user, userCred)
    
    def start(self):  
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('', self.port))
        sock.listen(1)
        self.logger.info('Starting Server on port ' + str(self.port))
        while True:
            newsocket, fromaddr = sock.accept()
            connstream = None
            try:
                connstream = ssl.wrap_socket(newsocket,
                              server_side=True,
                              ca_certs=self._ca_certs,
                              cert_reqs=ssl.CERT_REQUIRED,
                              certfile=self._certfile,
                              keyfile=self._keyfile,
                              ssl_version=ssl.PROTOCOL_TLSv1)
                self.process_client(connstream)
            except ssl.SSLError:
                self.logger.error("Unsuccessful connection attempt from: " + repr(fromaddr))
            except socket.error:
                self.logger.error("Error with the socket connection")
            #except:
            #    self.logger.error("Uncontrolled Error: " + str(sys.exc_info()))
            finally:
                if type(connstream) is ssl.SSLSocket:
                    try: 
                        connstream.shutdown(socket.SHUT_RDWR)
                        connstream.close()
                    except:
                        pass
                    

    def process_client(self, connstream):
        self.logger.info('Accepted new connection')
        #receive the message
        data = connstream.read(2048)
        self.logger.debug("received data: " + data)
        params = data.split('|')
        #params[0] is user
        #params[1] is passwd
        #params[2] is passwd type
        #params[3] is resource (cluster, node, service)
        #params[4] is operation (add, remove, create, list...)
        #params[5] is arguments. This can be a list or a string. We can use eval() if it is a list.
        #params[6] is foce. THis is to force remove/move node from service
        if len(params) != self.numparams:
            msg = "ERROR: incorrect message"
            self.errormsg(connstream, msg)
            return
        
        self.user = params[0]
        passwd = params[1]
        passwdtype = params[2]
        self.resource = params[3]
        self.operation = params[4]
        try:
            value = eval(params[5]) #try to convert in list or None.
            if value:
                self.arguments = value
            else:
                self.arguments = [None]
        except:
            self.arguments = [params[5]]  #here means the argument was an string
        
        try:
            self.forcemove = eval(params[6].strip())
        except:
            self.forcemove = False

        #FORCE MOVE, identify where to put that and decide if eveyone is sending this parameter or not (i think I should send it)

        retry = 0
        maxretry = 3
        endloop = False
        while (not endloop):            
            userCred = FGCredential(passwdtype, passwd)          
            if self.user in self.authorizedusers:
                if (self.auth(userCred)):
                    connstream.write("OK")                                        
                    endloop = True
                else:
                    retry += 1
                    if retry < maxretry:
                        connstream.write("TryAuthAgain")
                        passwd = connstream.read(2048)
                    else:
                        msg = "ERROR: authentication failed"
                        endloop = True
                        self.errormsg(connstream, msg)
                        return
            else:
                msg = "ERROR: authentication failed. User is not allowed to use this service."
                endloop = True
                self.errormsg(connstream, msg)
                return

        #load protected machines just in case it changed
        self.protectedMachines = self._rainConf.load_moveServerConfigCheckProtected()
        #print self.protectedMachines
        if self.resource == 'service' and (self.operation == "add" or self.operation == "remove" or self.operation == "move"
                                           or self.operation == "info"):

            nodelist = self.arguments[:len(self.arguments)-1]
            if self.operation == "move":
                nodelist = self.arguments[:len(self.arguments)-2]
                          
            full=False
            proc_list = []
            joinstatus = []            
            for node in nodelist:         
                if len(proc_list) == self.proc_max:
                    full = True
                    while full:
                        for i in range(len(proc_list) - 1, -1, -1):
                            #self.logger.debug(str(proc_list[i]))
                            if not proc_list[i].is_alive():
                                #print "dead"                      
                                proc_list.pop(i)
                                full = False
                        if full:
                            time.sleep(self.refresh_status)
                try:
                    if self.operation == "move":
                        new_arguments=[node,self.arguments[len(self.arguments)-2],self.arguments[len(self.arguments)-1]]
                    else:                    
                        new_arguments=[node,self.arguments[len(self.arguments)-1]]
                    
                        
                    proc_list.append(Thread(target=eval("self.wrap_" + self.operation), args=(joinstatus,new_arguments)))            
                    proc_list[len(proc_list) - 1].start()
                except:
                    status = "ERROR: Node with "+node+" in FG Move server process. " + str(sys.exc_info())
                    #self.errormsg(connstream, msg)
                    
            for i in proc_list:
                i.join()
            
            status = ""
            for i in joinstatus:
                status += i + "\n"
                if re.search("^ERROR", i):
                    self.logger.error(i)
                    
            self.okmsg(connstream, status)
            
            if self.operation != "info":
                #Put in the logs the current status of the services
                self.printCurrentStatus()
            
        else:
            
            try:
                
                status = eval("self." + self.operation + "("+str(self.arguments)+")")
                
            except:
                msg = "ERROR: incorrect operation " + str(sys.exc_info())
                self.errormsg(connstream, msg)
                return
            
            
            if re.search("^ERROR", status):
                #sends ERROR: ... 
                self.errormsg(connstream, status)
                #return
            else:
                
                #sends OK
                self.okmsg(connstream, status)
                
        
        self.logger.info("Rain Move Server DONE")
    
    def create(self, arguments):
        '''create empty clusters or services'''
        status = 'OK'
        
        if self.resource == 'cluster':
            if self.fgfabric.getCluster(self.arguments[0]) == None:
                self.fgfabric.addCluster(Cluster(self.arguments[0]))
                status = "The cluster has been successfully created."
            else:
                status = "ERROR: the Cluster already exists"
        elif self.resource == 'service':
            if self.fgfabric.getService(self.arguments[0]) == None:                
                if self.arguments[1].lower() == 'hpc':
                    success, msg = self.fgfabric.addService(HPCService(self.arguments[0]))
                elif self.arguments[1].lower() == 'eucalyptus':
                    success, msg = self.fgfabric.addService(EucaService(self.arguments[0]))
                elif self.arguments[1].lower() == 'openstack':
                    success, msg = self.fgfabric.addService(OpenStackService(self.arguments[0]))
                elif self.arguments[1].lower() == 'nimbus':
                    success, msg = self.fgfabric.addService(NimbusService(self.arguments[0]))
                elif self.arguments[1].lower() == 'opennebula':
                    success, msg = self.fgfabric.addService(OpenNebulaService(self.arguments[0]))
                if success:
                    status = "The service has been successfully created."
                else:
                    status = "ERROR: " + msg
            else:
                status = "ERROR: the Service already exists"
        
        return status
    
    def wrap_add(self, joinstatus, arguments):
        
        joinstatus.append(self.add(arguments))
    
    
    def add(self, arguments):
        '''add new node; existing node to a cluster; existing node to a service, etc.
        '''
        status = 'OK'
        # add a new node
        if self.resource == 'node':
            
            if arguments[0] in self.protectedMachines or arguments[1] in self.protectedMachines or arguments[2] in self.protectedMachines:
                status = "ERROR: the machine is protected"
            else:
                #construcing a node from args
                #accepting format of: id,name,ip,cluster
                newnode = Node(arguments[0], arguments[1], arguments[2], arguments[3])
    
                #ADD node to the node list
                if self.fgfabric.getNode(arguments[0]) == None:
                    cluster = self.fgfabric.getCluster(arguments[3])
                    if cluster != None:
                        self.fgfabric.addNode(newnode)
                        if not cluster.add(newnode):
                            status = 'ERROR: adding the cluster'
                    else:
                        status = "ERROR: the Node cannot be added because the Cluster does not exists"
                else:
                    status = "ERROR: the Node already exists"    
             
        # add a node to a service. This internally invokes the implementations for specific service types.    
        elif self.resource == 'service':
            existingnode = self.fgfabric.getNode(arguments[0])
            if existingnode != None:
                if arguments[0] in self.protectedMachines or existingnode.ip in self.protectedMachines or existingnode.name in self.protectedMachines:
                    status = "ERROR: the machine is protected"
                else:                    
                    service = self.fgfabric.getService(arguments[1])
                    if service != None:
                        success, restatus = service.add(existingnode)
                        if not success:
                            status = "ERROR: adding the node " + arguments[0] + " to the service " + arguments[1] + ". " + str(restatus)
                        else:
                            status = "The node " + arguments[0] + " have been successfully integrated into the service. " + arguments[1] + ". " + str(restatus)
                            try:      
                                self.fgfabric.addNode2Service(arguments[0],service)
                            except:
                                status = "ERROR: adding the node " + arguments[0] + ". Storing information in the persistent data in the Fabric. " + str(sys.exc_info())
                                                
                    else:
                        status = "ERROR: the Node " + arguments[0] + " cannot be added because the Service does not exists"
            else:
                status = "ERROR: the Node " + arguments[0] + " does not exists"

        return status
    
    def wrap_remove(self,joinstatus, arguments):
        
        joinstatus.append(self.remove(arguments))
    
    def remove(self, arguments):
        status = 'OK'
        if self.resource == 'node':
            #if arguments[0] in self.protectedMachines or arguments[1] in self.protectedMachines or arguments[2] in self.protectedMachines:
            #    status = "ERROR: the machine is protected"
            #else:
            status = "ERROR: Not supported yet"
        elif self.resource == 'cluster':
            status = "ERROR: Not supported yet"
        elif self.resource == 'service':  #Remove a node from a service
            if arguments[0] in self.protectedMachines:
                status = "ERROR: the machine is protected"
            else:
                service = self.fgfabric.getService(arguments[1])
                if service != None:
                    success, restatus = service.remove(arguments[0], self.forcemove)
                    if not success:
                        status = "ERROR: removing the node " + arguments[0] + " from the service " + arguments[1] + ". " + str(restatus)
                    else:
                        status = "The node " + arguments[0] + " have been successfully deleted from the service " + arguments[1] + ". " + str(restatus)
                        try:
                            self.fgfabric.removeNodeFromService(arguments[0],service)
                        except:
                            status = "ERROR: removing the node " + arguments[0] + ". Storing information in the persistent data in the Fabric. " + str(sys.exc_info())
                    
                else:
                    status = "ERROR: the Node " + arguments[0] + " cannot be deleted because the Service does not exists"
                        
        return status
    
    def wrap_move(self,joinstatus, arguments):
        
        joinstatus.append(self.move(arguments))
    
    def move(self, arguments):
        status = 'ERROR: Wrong resource.'
        if self.resource == 'service':
            status = self.remove(arguments)
            if not re.search("^ERROR",status):
                arguments[1] = arguments[2]
                status = self.add(arguments)           
                        
        return status
    
    def wrap_info(self,joinstatus, arguments):
        
        joinstatus.append(self.info(arguments))
    
    def info(self, arguments):
        if self.resource == 'node':
            if arguments[0] in self.fgfabric.getNode().keys():
                status = str(self.fgfabric.getNode()[arguments[0]])
            else:
                status = "ERROR: The node " + arguments[0] + " does not exists."
            
        elif self.resource == 'service':
            existingnode = self.fgfabric.getNode(arguments[0])
            if existingnode != None:
                service = self.fgfabric.getService(arguments[1])
                if service != None:
                    success, restatus = service.info(existingnode)
                    if not success:
                        status = "ERROR: Getting information of the node " + arguments[0] + " from the service " + arguments[1] + ". " + str(restatus)
                    else:
                        status = "Status of the node " + arguments[0] + ": " + str(restatus)
                else:
                    status = "ERROR: the Service " + arguments[1] + " does not exists"
            else:
                status = "ERROR: the Node " + arguments[0] + " does not exists"
                
        return status
    
    def lists(self, arguments):
        status = 'ERROR: Wrong resource.'
        if self.resource == 'cluster':
            if not arguments[0]: #print
                cluster = self.fgfabric.getCluster()
                status = "The list of clusters is: " + str(cluster.keys())                
            else:
                cluster = self.fgfabric.getCluster(arguments[0])
                if cluster != None:
                    status = "Details of cluster " + str(arguments[0]) + " cluster: " + str(cluster.list().keys())

        elif self.resource == 'service':
            if not arguments[0]: #print
                service = self.fgfabric.getService()
                status = "The list of services is: " + str(service.keys())
            else:
                service = self.fgfabric.getService(arguments[0])
                if service != None:
                    status = "Details of service " + str(arguments[0]) + " service: " + str(service.list().keys())
            
        return status

    def listfreenodes(self, arguments):
        status = "ERROR: getting the list of free nodes"
        
        dictfree={}
        
        if not arguments[0]: #print
            listcluster = self.fgfabric.getCluster()
            for i in listcluster:                
                dictfree[i]=[]
                cluster = self.fgfabric.getCluster(i)
                listnodes=cluster.list()
                for j in listnodes:
                    if listnodes[j].allocated == 'FREE':
                        dictfree[i].append(listnodes[j].identifier)
            status = str(dictfree)
        else:
            cluster = self.fgfabric.getCluster(arguments[0])
            dictfree[arguments[0]]=[]
            listnodes=cluster.list()
            for j in listnodes:
                if listnodes[j].allocated == 'FREE':
                    dictfree[i].append(listnodes[j].identifier)
                    
            status = str(dictfree)
        
        return status

    def printCurrentStatus(self):
        services_details={}
        services = self.fgfabric.getService()
        #print services
        
        for i in services.keys(): 
            serv = self.fgfabric.getService(i)
            services_details[i]=serv.list().keys()
            #"Details of service " + str(arguments[0]) + " service: " + str(service.list().keys())
            
        services_details['freenodes']=self.listfreenodes([None])
        self.logger.debug( "CURRENT_STATUS=" + str(services_details))
        
    def okmsg(self, connstream, msg):
        connstream.write(msg)
        connstream.shutdown(socket.SHUT_RDWR)
        connstream.close()

    def errormsg(self, connstream, msg):
        self.logger.error(msg)
        try:
            connstream.write(msg)
            connstream.shutdown(socket.SHUT_RDWR)
            connstream.close()
        except:
            self.logger.debug("In errormsg: " + str(sys.exc_info()))
        self.logger.info("Rain Move Server DONE")

def main():
    
    parser = argparse.ArgumentParser(prog="RainMoveServer", formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description="FutureGrid Rain Move Server Help ")    
    parser.add_argument('-l', '--load', dest='inventoryFile', metavar='inventoryFile',
                        help='File that contains the machines/services inventory')

    args = parser.parse_args()
    
    if args.inventoryFile != None:
        if not query_yes_no("You selected a file that contains the inventory. This will delete the MongoDB database specified in fg-server.conf (dbname). Do you want to continue?"):
            sys.exit()
    
    server = RainMoveServer(args.inventoryFile)
    server.start()

def query_yes_no(question, default="no"):
    """Ask a yes/no question via raw_input() and return their answer.

    "question" is a string that is presented to the user.
    "default" is the presumed answer if the user just hits <Enter>.
        It must be "yes" (the default), "no" or None (meaning
        an answer is required of the user).

    The "answer" return value is one of "yes" or "no".
    """
    valid = {"yes":True,   "y":True,  "ye":True,
             "no":False,     "n":False}
    if default == None:
        prompt = " [y/n] "
    elif default == "yes":
        prompt = " [Y/n] "
    elif default == "no":
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        sys.stdout.write(question + prompt)
        choice = raw_input().lower()
        if default is not None and choice == '':
            return valid[default]
        elif choice in valid:
            return valid[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' "\
                             "(or 'y' or 'n').\n")

if __name__ == "__main__":
    main()
#END
