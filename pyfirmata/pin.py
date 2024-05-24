class Pin(object):
    """A Pin representation"""
    def __init__(self, board, pin_number, type=ANALOG, port=None):
        self.board = board
        self.pin_number = pin_number
        self.type = type
        self.port = port
        self.PWM_CAPABLE = False
        self._mode = (type == DIGITAL and OUTPUT or INPUT)
        self.reporting = False
        self.value = None

    def __str__(self):
        type = {ANALOG: 'Analog', DIGITAL: 'Digital'}[self.type]
        return "{0} pin {1}".format(type, self.pin_number)

    def _set_mode(self, mode):
        if mode is UNAVAILABLE:
            self._mode = UNAVAILABLE
            return
        if self._mode is UNAVAILABLE:
            raise IOError("{0} can not be used through Firmata".format(self))
        if mode is PWM and not self.PWM_CAPABLE:
            raise IOError("{0} does not have PWM capabilities".format(self))
        if mode == SERVO:
            if self.type != DIGITAL:
                raise IOError("Only digital pins can drive servos! {0} is not"
                              "digital".format(self))
            self._mode = SERVO
            self.board.servo_config(self.pin_number)
            return

        # Set mode with SET_PIN_MODE message
        self._mode = mode
        self.board.sp.write(bytearray([SET_PIN_MODE, self.pin_number, mode]))
        if mode == INPUT:
            self.enable_reporting()

    def _get_mode(self):
        return self._mode

    mode = property(_get_mode, _set_mode)
    """
    Mode of operation for the pin. Can be one of the pin modes: INPUT, OUTPUT,
    ANALOG, PWM. or SERVO (or UNAVAILABLE).
    """

    def enable_reporting(self):
        """Set an input pin to report values."""
        if self.mode is not INPUT:
            raise IOError("{0} is not an input and can therefore not report".format(self))
        if self.type == ANALOG:
            self.reporting = True
            msg = bytearray([REPORT_ANALOG + self.pin_number, 1])
            self.board.sp.write(msg)
        else:
            self.port.enable_reporting()
            # TODO This is not going to work for non-optimized boards like Mega

    def disable_reporting(self):
        """Disable the reporting of an input pin."""
        if self.type == ANALOG:
            self.reporting = False
            msg = bytearray([REPORT_ANALOG + self.pin_number, 0])
            self.board.sp.write(msg)
        else:
            self.port.disable_reporting()
            # TODO This is not going to work for non-optimized boards like Mega

    def read(self):
        """
        Returns the output value of the pin. This value is updated by the
        boards :meth:`Board.iterate` method. Value is always in the range from
        0.0 to 1.0.
        """
        if self.mode == UNAVAILABLE:
            raise IOError("Cannot read pin {0}".format(self.__str__()))
        return self.value

    def write(self, value):
        """
        Output a voltage from the pin

        :arg value: Uses value as a boolean if the pin is in output mode, or
            expects a float from 0 to 1 if the pin is in PWM mode. If the pin
            is in SERVO the value should be in degrees.

        """
        if self.mode is UNAVAILABLE:
            raise IOError("{0} can not be used through Firmata".format(self))
        if self.mode is INPUT:
            raise IOError("{0} is set up as an INPUT and can therefore not be written to"
                          .format(self))
        if value is not self.value:
            self.value = value
            if self.mode is OUTPUT:
                if self.port:
                    self.port.write()
                else:
                    msg = bytearray([DIGITAL_MESSAGE, self.pin_number, value])
                    self.board.sp.write(msg)
            elif self.mode is PWM:
                value = int(round(value * 255))
                msg = bytearray([ANALOG_MESSAGE + self.pin_number, value % 128, value >> 7])
                self.board.sp.write(msg)
            elif self.mode is SERVO:
                value = int(value)
                msg = bytearray([ANALOG_MESSAGE + self.pin_number, value % 128, value >> 7])
                self.board.sp.write(msg)
