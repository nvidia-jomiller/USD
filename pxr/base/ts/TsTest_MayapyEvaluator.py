#
# Copyright 2024 Pixar
#
# Licensed under the terms set forth in the LICENSE.txt file available at
# https://openusd.org/license.
#

from pxr import Ts
import os, subprocess, threading, queue, selectors


class TsTest_MayapyEvaluator(object):
    """
    Evaluates spline data using mayapy.  Creates a child process that runs in
    mayapy, and communicates with it.
    """

    # XXX: would be nice to switch from text mode and readline to raw mode and
    # os.read.  This would allow us to just read whatever is available in a
    # pipe, without blocking.  This might improve reliability.  So far, however,
    # the readline solution is working; even if the child dies with an error,
    # complete lines of output are sent.

    # Override this method in a subclass in order to do something with log
    # messages.  This is only for debugging.  The messages passed to this method
    # are already newline-terminated.
    #
    # Note that this is one of two debug logs.  It is used for debug messages
    # from this process (the parent process).  The other log is for debug
    # messages from the child process, which runs in mayapy.  That log file is
    # specified in the subprocessDebugFilePath argument to the constructor.
    def _DebugLog(self, msg):
        pass

    def __init__(self, mayapyPath, subprocessDebugFilePath = None):

        self._stderrThread = None

        # Verify we can find mayapy, whose location is specified by our caller.
        assert os.path.isfile(mayapyPath)

        # Find the mayapy script that sits next to us.
        mayapyScriptPath = os.path.join(
            os.path.dirname(__file__), "TsTest_MayapyDriver.py")
        assert os.path.isfile(mayapyScriptPath)

        # Modify environment for child process.  Make I/O unbuffered so that we
        # immediately get any stderr output generated by the Maya guts.
        envDict = dict(os.environ)
        envDict["PYTHONUNBUFFERED"] = "1"

        # Create subprocess args: mayapy binary, script path, options.
        args = [mayapyPath, mayapyScriptPath]
        if subprocessDebugFilePath:
            args.append(subprocessDebugFilePath)

        # Start MayapyDriver in an asynchronous subprocess.
        self._DebugLog("Starting MayapyDriver...\n")
        self._DebugLog(str.join(" ", args) + "\n")
        self._mayapyProcess = subprocess.Popen(
            args,
            env = envDict,
            stdin = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE,
            text = True)
        if not self._IsMayapyRunning():
            raise Exception("Can't start MayapyDriver")

        # Start stderr reader thread for nonblocking reads.
        self._stderrQueue = queue.SimpleQueue()
        self._stderrThread = threading.Thread(
            target = self._StderrThreadMain)
        self._stderrThreadExit = False
        self._stderrThread.start()

        # Set up stdout polling interface.
        self._stdoutSelector = selectors.DefaultSelector()
        self._stdoutSelector.register(
            self._mayapyProcess.stdout, selectors.EVENT_READ)

        # Wait for MayapyDriver to signal readiness for input.
        # It sends an empty line on stdout after initialization.
        self._ReadFromChild()
        self._DebugLog("Done starting MayapyDriver\n")

    def __del__(self):
        self.Shutdown(wait = False)

    def Shutdown(self, wait = True):

        # Tell thread to exit.
        self._stderrThreadExit = True

        # Clean up child process.  This will unblock the thread.
        if self._IsMayapyRunning():
            self._DebugLog("Terminating MayapyDriver...\n")
            self._mayapyProcess.terminate()
            if wait:
                self._mayapyProcess.wait()
                self._mayapyProcess.stdin.close()
                self._mayapyProcess.stdout.close()
                self._mayapyProcess.stderr.close()
                self._DebugLog("Done terminating MayapyDriver\n")

        # Wait for reader thread to exit.
        if wait and self._stderrThread:
            self._stderrThread.join()

    def _IsMayapyRunning(self):
        """
        Return whether the MayapyDriver process is running and ready for input.
        """
        return self._mayapyProcess.poll() is None

    def _StderrThreadMain(self):
        """
        Repeatedly read from the child process' stderr, and post the resulting
        lines of text to the queue.
        """
        while True:
            # XXX: it seems like this readline() could hang forever if the child
            # dies, but in practice this hasn't happened.
            line = self._mayapyProcess.stderr.readline()
            if line:
                self._stderrQueue.put(line)
            if self._stderrThreadExit:
                break

    def _ReadFromChild(self):
        """
        Wait for child to either produce newline-terminated output on stdout, or
        die.  On success, return the output, including the newline.  On failure,
        raise an exception.
        """
        # Poll for output, which is newline-terminated.  Also poll for child
        # death.
        self._DebugLog("Waiting for MayapyDriver output...\n")
        stdoutStr = None
        while True:
            if not self._mayapyProcess.stdout.readable() \
                    or not self._IsMayapyRunning():
                # Child has died.
                break
            if self._stdoutSelector.select(timeout = 1):
                # Ready to read; assume we will be able read an entire line,
                # possibly after blocking.  The child could theoretically die in
                # the middle of sending output, but that hasn't been observed.
                stdoutStr = self._mayapyProcess.stdout.readline()
                break
        self._DebugLog("Done waiting for output\n")

        # If there is anything on stderr, display it.  This can happen whether
        # or not the child process has died.
        stderrLines = []
        while True:
            try:
                line = self._stderrQueue.get_nowait()
                if line:
                    stderrLines.append(line)
            except queue.Empty:
                break
        if stderrLines:
            self._DebugLog("MayapyDriver stderr:\n")
            for line in stderrLines:
                self._DebugLog(line)

        # If the child has died, dump any accumulated stdout, then bail.
        if not self._IsMayapyRunning():
            if stdoutStr:
                self._DebugLog("MayapyDriver stdout:\n")
                self._DebugLog(stdoutStr + "\n")
            raise Exception("MayapyDriver failure")

        return stdoutStr

    def Eval(self, data, times, opts = {}):
        """
        Send repr((data, times, opts)) to the MayapyDriver process.
        Wait; read, eval, and return result from the MayapyDriver process.
        These are the expected types, although they are opaque to this function:
        data: Ts.TestUtilsSplineData
        times: Ts.TestUtilsSampleTimes
        opts: dict
          autoTanMethod: 'auto' or 'smooth'
        return value: list(Ts.TsTest_Sample)
        """
        if not self._IsMayapyRunning():
            raise Exception("mayapy not running")

        # Send input.  Use repr for serialization.  The print() function
        # includes a newline terminator, which will signal end of input.
        inputStr = repr((data, times, opts))
        print(inputStr, file = self._mayapyProcess.stdin, flush = True)

        # Wait for output.  Deserialize with eval.
        outputStr = self._ReadFromChild()
        return eval(outputStr)
