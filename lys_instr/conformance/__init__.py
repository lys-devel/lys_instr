"""
Conformance checks for device implementations.

The functions in this package check whether a device implementation satisfies the requirements of lys_instr
(see "Requirements for Implementations" in the tutorials), such as thread safety and the busy state.
Device-specific parameters (e.g., positions to move to, tolerance, timeout) are given as arguments.

Example::

    from lys_instr.conformance import checkMotor, checkDetector

    report = checkMotor(motor, "x", start=0, target=1, tolerance=1e-3, stopTarget=100)
    print(report)
    report.raiseIfFailed()

The checks move the devices and acquire data. Run them on devices that are safe to operate,
and do not connect the detector to a DataStorage while checking.
"""

from ._common import ConformanceReport, CheckResult
from .controller import checkMotor, checkSwitch
from .detector import checkDetector
