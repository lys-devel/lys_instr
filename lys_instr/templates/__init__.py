from lys_instr.MultiDetector import DetectorInterface


def releaseOnClose(window, devices):
    """
    Stop the devices and their monitoring threads when the window is closed, so that no threads are left after closing.

    Args:
        window (LysSubWindow): The window.
        devices (list[HardwareInterface]): Devices owned by the window.
    """
    def release(*args):
        for device in devices:
            if isinstance(device, DetectorInterface):
                if device.isBusy:
                    device.stop()
            elif any(device.isBusy.values()):
                device.stop()
            device.kill()
            device.wait(5000)
    window.closed.connect(release)



def test():
    from .template1 import Window
    return Window()