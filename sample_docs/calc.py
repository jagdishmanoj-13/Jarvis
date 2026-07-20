def compute_torque(bolt_size):
    """Return torque in Nm for a given bolt size."""
    return {'M6':12,'M8':25}.get(bolt_size)

class TorqueTable:
    def lookup(self, size):
        return compute_torque(size)
