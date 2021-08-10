#
# auto-pts - The Bluetooth PTS Automation Framework
#
# Copyright (c) 2017, Intel Corporation
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of Intel Corporation nor the names of its contributors
#       may be used to endorse or promote products derived from this software
#       without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#

import argparse
import copy
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import xmlrpc.client
import xmlrpc.server
from os.path import dirname, abspath
from queue import Queue
from time import sleep

import pythoncom
import wmi
import winutils

import ptscontrol
from config import SERVER_PORT

log = logging.debug
PROJECT_DIR = dirname(abspath(__file__))


class PyPTSWithXmlRpcCallback(ptscontrol.PyPTS):
    """A child class that adds support of xmlrpc PTS callbacks to PyPTS"""

    def __init__(self):
        """Constructor"""
        super().__init__()

        log("%s", self.__init__.__name__)

        # address of the auto-pts client that started it's own xmlrpc server to
        # receive callback messages
        self.client_address = None
        self.client_port = None
        self.client_xmlrpc_proxy = None

    def register_xmlrpc_ptscallback(self, client_address, client_port):
        """Registers client callback. xmlrpc proxy/client calls this method
        to register its callback

        client_address -- IP address
        client_port -- TCP port
        """

        log("%s %s %d", self.register_xmlrpc_ptscallback.__name__,
            client_address, client_port)

        self.client_address = client_address
        self.client_port = client_port

        self.client_xmlrpc_proxy = xmlrpc.client.ServerProxy(
            "http://{}:{}/".format(self.client_address, self.client_port),
            allow_none=True)

        log("Created XMR RPC auto-pts client proxy, provides methods: %s" %
            self.client_xmlrpc_proxy.system.listMethods())

        self.register_ptscallback(self.client_xmlrpc_proxy)

    def unregister_xmlrpc_ptscallback(self):
        """Unregisters the client callback"""

        log("%s", self.unregister_xmlrpc_ptscallback.__name__)

        self.unregister_ptscallback()

        self.client_address = None
        self.client_port = None
        self.client_xmlrpc_proxy = None


class SvrArgumentParser(argparse.ArgumentParser):
    def __init__(self, description):
        argparse.ArgumentParser.__init__(self, description=description)

        self.add_argument("-S", "--srv_port", type=int,
                          nargs="+", default=[SERVER_PORT],
                          help="Specify the server port number")

        self.add_argument("--recovery", action='store_true', default=False,
                          help="Specify if autoptsserver should try to recover"
                          " itself after exception.")

        self.add_argument("--superguard", default=0, type=float, metavar='MINUTES',
                          help="Specify amount of time in minutes, after which"
                          " super guard will blindly trigger recovery steps.")

        self.add_argument("--ykush", nargs="+", default=[], metavar='YKUSH_PORT',
                          help="Specify ykush hub downstream port number, so "
                          "during recovery steps PTS dongle could be replugged.")

    @staticmethod
    def check_args(arg):
        """Sanity check command line arguments"""

        for srv_port in arg.srv_port:
            if not 49152 <= srv_port <= 65535:
                sys.exit("Invalid server port number=%s, expected range <49152,65535> " % (srv_port,))

        if len(arg.srv_port) == 1:
            arg.srv_port = arg.srv_port[0]

        arg.superguard = 60 * arg.superguard

    def parse_args(self, args=None, namespace=None):
        arg = super().parse_args()
        self.check_args(arg)
        return arg


def get_workspace(workspace):
    for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, 'workspaces'),
                                     topdown=True):
        for name in dirs:
            if name == workspace:
                return os.path.join(root, name)
    return None


def kill_all_processes(name):
    c = wmi.WMI()
    for ps in c.Win32_Process(name=name):
        try:
            ps.Terminate()
            log("%s process (PID %d) terminated successfully" % (name, ps.ProcessId))
        except BaseException as exc:
            logging.exception(exc)
            log("There is no %s process running with id: %d" % (name, ps.ProcessId))


def delete_workspaces():
    def recursive(directory, depth):
        depth -= 1
        with os.scandir(directory) as iterator:
            for f in iterator:
                if f.is_dir() and depth > 0:
                    recursive(f.path, depth)
                elif f.name.startswith('temp_') and f.name.endswith('.pqw6'):
                    os.remove(f)

    init_depth = 4
    recursive(os.path.join(PROJECT_DIR, 'workspaces'), init_depth)


def recover_pts(ykush_ports=None):
    print("Recovering PTS ...")
    kill_all_processes("PTS.exe")
    kill_all_processes("Fts.exe")
    delete_workspaces()
    if ykush_ports:
        turn_on_dongle(ykush_ports)


def turn_on_dongle(ykush_ports):
    ykushcmd = 'ykushcmd'
    if sys.platform == "win32":
        ykushcmd += '.exe'

    for port in ykush_ports:
        subprocess.Popen([ykushcmd, '-d', str(port)], stdout=subprocess.PIPE)
        print('Repluging PTS dongle on ykush port', str(port))

    time.sleep(5)

    for port in ykush_ports:
        subprocess.Popen([ykushcmd, '-u', str(port)], stdout=subprocess.PIPE)

    time.sleep(2)


class SuperGuard(threading.Thread):
    def __init__(self, timeout, _queue):
        threading.Thread.__init__(self, daemon=True)
        self.servers = []
        self.queue = _queue
        self.timeout = timeout
        self.end = False
        self.was_timeout = False

    def run(self):
        while not self.end:
            idle_num = 0
            for srv in self.servers:
                if time.time() - srv.last_start() > self.timeout:
                    idle_num += 1

            if idle_num == len(self.servers) and idle_num != 0:
                for srv in self.servers:
                    srv.terminate('Superguard timeout')
                self.was_timeout = True
                self.servers.clear()
            sleep(5)

    def clear(self):
        self.servers.clear()
        self.was_timeout = False

    def add_server(self, srv):
        self.servers.append(srv)

    def terminate(self):
        self.end = True


class Server(threading.Thread):
    def __init__(self, _args=None, _queue=None):
        threading.Thread.__init__(self, daemon=True)
        self.queue = _queue
        self.server = None
        self._args = _args
        self.pts = None

    def last_start(self):
        if self.pts:
            return self.pts.last_start_time
        return time.time()

    def main(self, _args):
        """Main."""
        pythoncom.CoInitialize()
        script_name = os.path.basename(sys.argv[0])  # in case it is full path
        script_name_no_ext = os.path.splitext(script_name)[0]

        log_filename = "%s_%s.log" % (script_name_no_ext, str(_args.srv_port))
        format_template = "%(asctime)s %(name)s %(levelname)s : %(message)s"

        logging.basicConfig(format=format_template,
                            filename=log_filename,
                            filemode='a',
                            level=logging.DEBUG)

        c = wmi.WMI()
        for iface in c.Win32_NetworkAdapterConfiguration(IPEnabled=True):
            print("Local IP address: %s DNS %r" % (iface.IPAddress, iface.DNSDomain))

        print("Starting PTS ...")
        self.pts = PyPTSWithXmlRpcCallback()
        print("OK")

        print("Serving on port {} ...".format(_args.srv_port))

        self.server = xmlrpc.server.SimpleXMLRPCServer(("", _args.srv_port), allow_none=True)
        self.server.register_function(self.request_recovery, 'request_recovery')
        self.server.register_function(self.list_workspace_tree, 'list_workspace_tree')
        self.server.register_function(self.copy_file, 'copy_file')
        self.server.register_function(self.delete_file, 'delete_file')
        self.server.register_instance(self.pts)
        self.server.register_introspection_functions()
        self.server.serve_forever()
        self.server.server_close()
        return 0

    def run(self):
        try:
            self.main(self._args)
        except Exception as exc:
            logging.exception(exc)
            print('Server ', str(self._args.srv_port), ' finished')
            self.terminate('from Server process on port ' +
                           str(self._args.srv_port) + ':\n' + traceback.format_exc())

    def request_recovery(self):
        self.terminate('Recovery request')

    def terminate(self, msg):
        try:
            if self.server:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        except BaseException as exc:
            logging.exception(exc)
            traceback.print_exc()
        if self.queue:
            self.queue.put(Exception(msg))

    def list_workspace_tree(self, workspace_dir):
        # self.pts.last_start_time = time.time()
        logs_root = get_workspace(workspace_dir)
        file_list = []
        for root, dirs, files in os.walk(logs_root,
                                         topdown=False):
            for name in files:
                file_list.append(os.path.join(root, name))

            file_list.append(root)

        return file_list

    def copy_file(self, file_path):
        # self.pts.last_start_time = time.time()
        file_bin = None
        if os.path.isfile(file_path):
            with open(file_path, 'rb') as handle:
                file_bin = xmlrpc.client.Binary(handle.read())
        return file_bin

    def delete_file(self, file_path):
        # self.pts.last_start_time = time.time()
        if os.path.isfile(file_path):
            os.remove(file_path)
        elif os.path.isdir(file_path):
            shutil.rmtree(file_path, ignore_errors=True)


def multi_main(_args, _queue, _superguard):
    """Multi server main."""

    servers = []
    for port in _args.srv_port:
        args_copy = copy.deepcopy(_args)
        args_copy.srv_port = port
        srv = Server(_args=args_copy, _queue=_queue)
        servers.append(srv)
        srv.start()
        superguard.add_server(srv)
        sleep(5)

    while _queue.empty():
        for srv in servers:
            if not srv.is_alive():
                _queue.put(Exception('Server is down'))
        sleep(2)  # This loop has a huge impact on the performance of server threads

    for s in servers:
        s.terminate('')


if __name__ == "__main__":
    winutils.exit_if_admin()
    _args = SvrArgumentParser("PTS automation server").parse_args()
    queue = Queue()

    with os.scandir(PROJECT_DIR) as it:
        for file in it:
            if file.name.startswith('autoptsserver_') and file.name.endswith('.log'):
                os.remove(file)

    superguard = SuperGuard(float(_args.superguard), queue)
    if _args.superguard:
        superguard.start()

    while True:
        try:
            if isinstance(_args.srv_port, int):
                server = Server(_queue=queue)
                superguard.add_server(server)

                server.main(_args)  # Run server in main process
            else:
                multi_main(_args, queue, superguard)  # Run many servers in threads

            exceptions = ''
            while not queue.empty():
                try:
                    exceptions += str(queue.get_nowait()) + '\n'
                except BaseException as ex:
                    logging.exception(ex)
                    traceback.print_exc()

            if exceptions != '':
                raise Exception(exceptions)
            break

        except KeyboardInterrupt:  # Ctrl-C
            sys.exit(14)

        except BaseException as e:
            logging.exception(e)
            traceback.print_exc()
            if _args.recovery or superguard.was_timeout:
                superguard.clear()
                recover_pts(_args.ykush)
            else:
                sys.exit(16)
