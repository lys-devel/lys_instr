
Lifecycle and Threads
=====================

This page explains the lifecycle of motors (``MultiMotorInterface``, ``MultiSwitchInterface``) and detectors (``MultiDetectorInterface``):
which threads are created and when, which methods run in which thread, and from which thread each signal is emitted.
It is useful when you implement a device, connect your own code to the signals, or control the devices from scripts and other threads.

See "Requirements for Implementations" in :doc:`Motor </lys_instr_/tutorial_/motor>` and :doc:`Detector </lys_instr_/tutorial_/detector>`
for the rules that device implementations must follow.


Threads
-------

The following threads are involved.

.. list-table::
   :header-rows: 1
   :widths: 22 38 40

   * - Thread
     - Created / finished
     - What runs in it
   * - **Caller thread**
     - The thread that calls the public methods (usually the main (GUI) thread, but it may be any thread, e.g., a script thread).
     - ``set()``, ``get()``, ``stop()``, ``startAcq()``, ``waitForReady()`` and the device methods they call (``_set()``, ``_get()``, ``_stop()``, ...).
   * - **Monitoring thread**
     - One per device. Started when the device calls ``self.start()`` at the end of its ``__init__()``. Finished by ``kill()``.
     - ``_loadState()`` at every ``interval`` (default 0.1 s): ``_isBusy()``, ``_get()`` and ``_isAlive()`` for motors, ``_isAlive()`` for detectors.
   * - **Acquisition thread**
     - Detectors only. Created and started by every ``startAcq()``. Finished when ``_run()`` returns.
     - ``_run()``, and ``_get()`` every time ``updated`` is emitted.

The device object itself (a ``QThread`` subclass) belongs to the thread that created it, usually the main thread.
Therefore, slots of the device object called through queued connections run in the main thread.

Because device methods are called from these threads at the same time, device implementations must be thread-safe
(see "Requirements for Implementations").


Motors
------

Creation
^^^^^^^^

1. ``__init__()`` of ``MultiControllerInterface`` initializes the state of the axes (busy, alive, target) and registers the device.
   It also connects ``QCoreApplication.aboutToQuit`` so that the monitoring thread is stopped when the application quits.
2. Your subclass establishes the connection to the instrument and calls ``self.start()``, which starts the monitoring thread.

Monitoring (monitoring thread)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Every ``interval`` seconds, ``_loadState()`` runs in the monitoring thread:

1. Read the busy state of the axes recorded by lys_instr, and a counter that is incremented by every ``set()`` (with the instance mutex).
2. Call ``_isBusy()``, ``_get()`` (only if any axis is or was busy) and ``_isAlive()`` **without** holding the mutex,
   so that ``set()`` called from other threads is not blocked by the communication with the instrument.
3. With the mutex held:

   - Emit ``valueChanged`` with the positions of the busy (or just finished) axes.
   - If ``set()`` was not called during step 2, update the busy state and emit ``busyStateChanged`` for the axes whose busy state changed.
     Otherwise the busy state is discarded, because it may be older than the command, and it is updated in the next cycle.
   - Emit ``aliveStateChanged`` if the alive state changed.

``set()`` (caller thread)
^^^^^^^^^^^^^^^^^^^^^^^^^

1. Acquire the instance mutex (unless ``lock=False``).
2. Validate the axis names, record the targets, mark the axes busy, and emit ``busyStateChanged`` (``True``) for the axes that were not busy.
3. Call ``_set()`` to send the command to the instrument.
4. Increment the counter and release the mutex.
5. If ``wait=True``, call ``waitForReady()``.

``get()``, ``stop()`` and ``isAlive`` call ``_get()``, ``_stop()`` and ``_isAlive()`` directly in the caller thread.
``isBusy`` calls ``_isBusy()`` in the caller thread while holding the mutex.

``waitForReady()`` (caller thread)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

1. Ask the instrument by ``_isBusy()``. If no axis is busy, return immediately.
2. Otherwise, run a nested Qt event loop in the caller thread until the monitoring thread reports that all axes are idle
   (``busyStateChanged``), or until the monitoring thread finishes (e.g., ``kill()``).

Because the event loop is running, the GUI is not frozen while waiting, and other events (button clicks, timers, remote commands) are processed.

End
^^^

``kill()`` asks the monitoring thread to finish after the current cycle. The device keeps a reference to itself until the thread finishes,
because destroying a running ``QThread`` aborts the application. ``kill()`` does not stop the motion; call ``stop()`` for that.

Signals
^^^^^^^

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Signal
     - Emitted from
     - When
   * - ``busyStateChanged(dict)``
     - Caller thread (``True``), monitoring thread (``True``/``False``)
     - ``True`` when ``set()`` starts a motion of an idle axis; changes detected by polling.
   * - ``valueChanged(dict)``
     - Monitoring thread; the thread that created the motor (usually main)
     - At every polling while an axis is busy, and when the motion finishes; when an offset is changed (relayed by a function connected in ``__init__()``).
   * - ``aliveStateChanged(dict)``
     - Monitoring thread
     - When the alive state of any axis changes.
   * - ``offsetChanged()``
     - The thread changing ``offset``
     - When an offset is changed.

``busyStateChanged`` and ``valueChanged`` emitted by the monitoring thread are emitted while holding the instance mutex.
When an offset is changed, the offsets are saved to a file and ``valueChanged`` is emitted in the thread that created the motor.


Detectors
---------

Creation
^^^^^^^^

1. ``__init__()`` of ``DetectorInterface`` initializes the busy state and the exposure, and registers the device.
2. Your subclass establishes the connection to the instrument and calls ``self.start()``, which starts the monitoring thread.
   The monitoring thread only checks ``_isAlive()``.

Acquisition
^^^^^^^^^^^

``startAcq(iter, wait, output)`` runs the following steps.

.. list-table::
   :header-rows: 1
   :widths: 6 24 70

   * - Step
     - Thread
     - What happens
   * - 1
     - Caller
     - If the detector is busy, log a warning and return.
   * - 2
     - Caller
     - Emit ``startRequested({"iter": iter, "errors": []})``. Listeners connected with ``DirectConnection`` (e.g., ``DataStorage``) check whether the data can be saved, and append reasons to ``errors`` to reject the acquisition. If rejected, ``AcquisitionRejectedError`` is raised and nothing else happens.
   * - 3
     - Caller
     - Set the busy state and ``iteration``, and emit ``busyStateChanged(True)``. ``DataStorage`` reserves a file here.
   * - 4
     - Caller
     - Wait for the previous acquisition thread to finish (it has already finished in most cases), create a new acquisition thread and start it.
   * - 5
     - Acquisition
     - Call ``_run(iter)``. Every time ``_run()`` emits ``updated``, ``_get()`` is called in the thread that emitted it (usually the acquisition thread), and ``dataAcquired`` is emitted with the frames.
   * - 6
     - Acquisition
     - After ``_run()`` returns, call ``_get()`` once more and emit ``dataAcquired`` with the remaining frames.
   * - 7
     - Acquisition
     - When the thread finishes, set the busy state to idle and emit ``busyStateChanged(False)``.
   * - 8
     - Caller
     - If ``wait=True``, a nested event loop runs in the caller thread until step 7 is delivered. If ``output=True``, the frames are collected and returned.

``iter=-1`` means a continuous acquisition (streaming), which continues until ``stop()`` is called. ``DataStorage`` does not save streaming.

``stop()`` (caller thread)
^^^^^^^^^^^^^^^^^^^^^^^^^^

1. Call ``_stop()``. ``_run()`` must return promptly after this.
2. If the acquisition thread is running, wait for it to finish with a nested event loop (the GUI is not frozen).
3. Call ``_get()`` and emit ``dataAcquired`` with the remaining frames (from the caller thread).
4. Post ``stopped`` to the event queue of the thread that the detector belongs to (usually the main thread).
   It is emitted after the frames emitted by the acquisition thread are delivered, so that ``DataStorage`` saves all frames.

End
^^^

``kill()`` finishes the monitoring thread as for motors. It does not stop the acquisition; call ``stop()`` first.

Signals
^^^^^^^

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Signal
     - Emitted from
     - When
   * - ``startRequested(dict)``
     - Caller thread
     - At the beginning of ``startAcq()``, before the acquisition starts.
   * - ``busyStateChanged(bool)``
     - Caller thread (``True``), acquisition thread (``False``)
     - When the acquisition starts and when the acquisition thread finishes.
   * - ``updated()``
     - The thread in which ``_run()`` emits it (usually the acquisition thread)
     - Emitted by your implementation when new frames are stored.
   * - ``dataAcquired(dict)``
     - Acquisition thread; caller thread of ``stop()``
     - After every ``updated``, after ``_run()`` returns, and in ``stop()``.
   * - ``stopped()``
     - Thread of the detector object (usually main), through the event queue
     - After ``stop()``.
   * - ``aliveStateChanged(bool)``
     - Monitoring thread
     - When the alive state changes.


Receiving Signals
-----------------

Which thread runs your slot depends on the connection type:

- **Auto connection** (the default) to a method of a ``QObject`` (e.g., a widget): the slot runs in the thread that the object belongs to.
  If the signal is emitted from another thread, the call is queued and executed by the event loop of that thread.
- **Auto connection** to a function or lambda: PyQt runs it in the thread where ``connect()`` was called.
- **Direct connection** (``QtCore.Qt.DirectConnection``): the slot runs immediately in the emitting thread.
  Use it only when the slot must finish before the emitter continues (e.g., ``startRequested``), and make the slot thread-safe.

In lys_instr:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - Receiver
     - Signals
     - Thread of the slot
   * - GUI widgets (``MultiMotorGUI``, ``MultiDetectorGUI``, ...)
     - value, busy, alive and data signals
     - Main thread (auto connection).
   * - ``DataStorage``
     - ``dataAcquired``, ``busyStateChanged``, ``stopped``
     - Main thread (lambdas connected in the main thread).
   * - ``DataStorage``
     - ``startRequested``
     - Caller thread of ``startAcq()`` (direct connection).
   * - ``PreCorrector``
     - ``busyStateChanged`` of motors
     - Emitting thread, while holding the mutex of the motor (direct connection).

Because the signals emitted by one thread are delivered to a receiver in another thread in the order of emission,
a receiver in the main thread always receives all ``dataAcquired`` of an acquisition before its ``busyStateChanged(False)``, and ``stopped`` after them.
The data in ``dataAcquired`` is passed by reference. Do not modify the frames in a receiver, and do not reuse the arrays in your implementation after storing them.


Waiting and Re-entrance
-----------------------

``set(wait=True)``, ``waitForReady()``, ``startAcq(wait=True)`` and ``stop()`` wait by running a nested event loop in the caller thread.
The GUI stays responsive, but other events are processed while waiting.
For example, a button may be clicked or a remote command may be executed in the middle of ``set(wait=True)``.
Write your slots so that they can be called at such times.

These methods may also be called from threads other than the main thread.
Qt runs the nested event loop in that thread, so the waiting thread does not need its own event loop.

Scans (``ScanWidget``) run in the main thread: each point calls ``set(wait=True)`` of the motors and then ``startAcq()`` of the detector,
and the next point starts when ``busyStateChanged(False)`` of the detector is delivered.
The file name of each point is made from the target values set by the scan, without reading the devices.


Closing and Quitting
--------------------

- **Closing a window**: ``ScanWidget`` stops the running scan when its sub window is closed, before the window is destroyed.
  Windows created from the templates stop and ``kill()`` the devices they created (``releaseOnClose``), and wait for the monitoring threads to finish.
  If you create your own window, do the same for the devices it owns.
- **Quitting the application**: on ``QCoreApplication.aboutToQuit``, running scans are stopped, every device kills its monitoring thread and waits for it,
  and ``DataStorage`` waits for the files being written.
- **References**: keep a reference to each device while its threads are running.
  lys_instr keeps internal references until the threads finish, but do not rely on garbage collection to stop a device; call ``kill()``.
