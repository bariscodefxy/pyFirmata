class Board(object):
    """The Base class for any board."""
    firmata_version = None
    firmware = None
    firmware_version = None
    _command_handlers = {}
    _command = None
    _stored_data = []
    _parsing_sysex = False

    def __init__(self, port, layout=None, baudrate=57600, name=None, timeout=None):
        self.sp = serial.Serial(port, baudrate, timeout=timeout)
        # Allow 5 secs for Arduino's auto-reset to happen
        # Alas, Firmata blinks its version before printing it to serial
        # For 2.3, even 5 seconds might not be enough.
        # TODO Find a more reliable way to wait until the board is ready
        self.pass_time(BOARD_SETUP_WAIT_TIME)
        self.name = name
        self._layout = layout
        if not self.name:
            self.name = port

        if layout:
            self.setup_layout(layout)
        else:
            self.auto_setup()

        # Iterate over the first messages to get firmware data
        while self.bytes_available():
            self.iterate()
        # TODO Test whether we got a firmware name and version, otherwise there
        # probably isn't any Firmata installed

    def __str__(self):
        return "Board{0.name} on {0.sp.port}".format(self)

    def __del__(self):
        """
        The connection with the a board can get messed up when a script is
        closed without calling board.exit() (which closes the serial
        connection). Therefore also do it here and hope it helps.
        """
        self.exit()

    def send_as_two_bytes(self, val):
        self.sp.write(bytearray([val % 128, val >> 7]))

    def setup_layout(self, board_layout):
        """
        Setup the Pin instances based on the given board layout.
        """
        # Create pin instances based on board layout
        self.analog = []
        for i in board_layout['analog']:
            self.analog.append(Pin(self, i))

        self.digital = []
        self.digital_ports = []
        for i in range(0, len(board_layout['digital']), 8):
            num_pins = len(board_layout['digital'][i:i + 8])
            port_number = int(i / 8)
            self.digital_ports.append(Port(self, port_number, num_pins))

        # Allow to access the Pin instances directly
        for port in self.digital_ports:
            self.digital += port.pins

        # Setup PWM pins
        for i in board_layout['pwm']:
            self.digital[i].PWM_CAPABLE = True

        # Disable certain ports like Rx/Tx and crystal ports
        for i in board_layout['disabled']:
            self.digital[i].mode = UNAVAILABLE

        # Create a dictionary of 'taken' pins. Used by the get_pin method
        self.taken = {'analog': dict(map(lambda p: (p.pin_number, False), self.analog)),
                      'digital': dict(map(lambda p: (p.pin_number, False), self.digital))}

        self._set_default_handlers()

    def _set_default_handlers(self):
        # Setup default handlers for standard incoming commands
        self.add_cmd_handler(ANALOG_MESSAGE, self._handle_analog_message)
        self.add_cmd_handler(DIGITAL_MESSAGE, self._handle_digital_message)
        self.add_cmd_handler(REPORT_VERSION, self._handle_report_version)
        self.add_cmd_handler(REPORT_FIRMWARE, self._handle_report_firmware)

    def auto_setup(self):
        """
        Automatic setup based on Firmata's "Capability Query"
        """
        self.add_cmd_handler(CAPABILITY_RESPONSE, self._handle_report_capability_response)
        self.send_sysex(CAPABILITY_QUERY, [])
        self.pass_time(0.1)  # Serial SYNC

        while self.bytes_available():
            self.iterate()

        # handle_report_capability_response will write self._layout
        if self._layout:
            self.setup_layout(self._layout)
        else:
            raise IOError("Board detection failed.")

    def add_cmd_handler(self, cmd, func):
        """Adds a command handler for a command."""
        len_args = len(inspect.getfullargspec(func)[0])

        def add_meta(f):
            def decorator(*args, **kwargs):
                f(*args, **kwargs)
            decorator.bytes_needed = len_args - 1  # exclude self
            decorator.__name__ = f.__name__
            return decorator
        func = add_meta(func)
        self._command_handlers[cmd] = func

    def get_pin(self, pin_def):
        """
        Returns the activated pin given by the pin definition.
        May raise an ``InvalidPinDefError`` or a ``PinAlreadyTakenError``.

        :arg pin_def: Pin definition as described below,
            but without the arduino name. So for example ``a:1:i``.

        'a' analog pin     Pin number   'i' for input
        'd' digital pin    Pin number   'o' for output
                                        'p' for pwm (Pulse-width modulation)

        All seperated by ``:``.
        """
        if type(pin_def) == list:
            bits = pin_def
        else:
            bits = pin_def.split(':')
        a_d = bits[0] == 'a' and 'analog' or 'digital'
        part = getattr(self, a_d)
        pin_nr = int(bits[1])
        if pin_nr >= len(part):
            raise InvalidPinDefError('Invalid pin definition: {0} at position 3 on {1}'
                                     .format(pin_def, self.name))
        if getattr(part[pin_nr], 'mode', None) == UNAVAILABLE:
            raise InvalidPinDefError('Invalid pin definition: '
                                     'UNAVAILABLE pin {0} at position on {1}'
                                     .format(pin_def, self.name))
        if self.taken[a_d][pin_nr]:
            raise PinAlreadyTakenError('{0} pin {1} is already taken on {2}'
                                       .format(a_d, bits[1], self.name))
        # ok, should be available
        pin = part[pin_nr]
        self.taken[a_d][pin_nr] = True
        if pin.type is DIGITAL:
            if bits[2] == 'p':
                pin.mode = PWM
            elif bits[2] == 's':
                pin.mode = SERVO
            elif bits[2] != 'o':
                pin.mode = INPUT
        else:
            pin.enable_reporting()
        return pin

    def pass_time(self, t):
        """Non-blocking time-out for ``t`` seconds."""
        cont = time.time() + t
        while time.time() < cont:
            time.sleep(0)

    def send_sysex(self, sysex_cmd, data):
        """
        Sends a SysEx msg.

        :arg sysex_cmd: A sysex command byte
        : arg data: a bytearray of 7-bit bytes of arbitrary data
        """
        msg = bytearray([START_SYSEX, sysex_cmd])
        msg.extend(data)
        msg.append(END_SYSEX)
        self.sp.write(msg)

    def bytes_available(self):
        return self.sp.inWaiting()

    def iterate(self):
        """
        Reads and handles data from the microcontroller over the serial port.
        This method should be called in a main loop or in an :class:`Iterator`
        instance to keep this boards pin values up to date.
        """
        byte = self.sp.read()
        if not byte:
            return
        data = ord(byte)
        received_data = []
        handler = None
        if data < START_SYSEX:
            # These commands can have 'channel data' like a pin nummber appended.
            try:
                handler = self._command_handlers[data & 0xF0]
            except KeyError:
                return
            received_data.append(data & 0x0F)
            while len(received_data) < handler.bytes_needed:
                received_data.append(ord(self.sp.read()))
        elif data == START_SYSEX:
            data = ord(self.sp.read())
            handler = self._command_handlers.get(data)
            if not handler:
                return
            data = ord(self.sp.read())
            while data != END_SYSEX:
                received_data.append(data)
                data = ord(self.sp.read())
        else:
            try:
                handler = self._command_handlers[data]
            except KeyError:
                return
            while len(received_data) < handler.bytes_needed:
                received_data.append(ord(self.sp.read()))
        # Handle the data
        try:
            handler(*received_data)
        except ValueError:
            pass

    def get_firmata_version(self):
        """
        Returns a version tuple (major, minor) for the firmata firmware on the
        board.
        """
        return self.firmata_version

    def servo_config(self, pin, min_pulse=544, max_pulse=2400, angle=0):
        """
        Configure a pin as servo with min_pulse, max_pulse and first angle.
        ``min_pulse`` and ``max_pulse`` default to the arduino defaults.
        """
        if pin > len(self.digital) or self.digital[pin].mode == UNAVAILABLE:
            raise IOError("Pin {0} is not a valid servo pin".format(pin))

        data = bytearray([pin])
        data += to_two_bytes(min_pulse)
        data += to_two_bytes(max_pulse)
        self.send_sysex(SERVO_CONFIG, data)

        # set pin._mode to SERVO so that it sends analog messages
        # don't set pin.mode as that calls this method
        self.digital[pin]._mode = SERVO
        self.digital[pin].write(angle)

    def exit(self):
        """Call this to exit cleanly."""
        # First detach all servo's, otherwise it somehow doesn't want to close...
        if hasattr(self, 'digital'):
            for pin in self.digital:
                if pin.mode == SERVO:
                    pin.mode = OUTPUT
        if hasattr(self, 'sp'):
            self.sp.close()

    # Command handlers
    def _handle_analog_message(self, pin_nr, lsb, msb):
        value = round(float((msb << 7) + lsb) / 1023, 4)
        # Only set the value if we are actually reporting
        try:
            if self.analog[pin_nr].reporting:
                self.analog[pin_nr].value = value
        except IndexError:
            raise ValueError

    def _handle_digital_message(self, port_nr, lsb, msb):
        """
        Digital messages always go by the whole port. This means we have a
        bitmask which we update the port.
        """
        mask = (msb << 7) + lsb
        try:
            self.digital_ports[port_nr]._update(mask)
        except IndexError:
            raise ValueError

    def _handle_report_version(self, major, minor):
        self.firmata_version = (major, minor)

    def _handle_report_firmware(self, *data):
        major = data[0]
        minor = data[1]
        self.firmware_version = (major, minor)
        self.firmware = two_byte_iter_to_str(data[2:])

    def _handle_report_capability_response(self, *data):
        charbuffer = []
        pin_spec_list = []

        for c in data:
            if c == CAPABILITY_RESPONSE:
                continue

            charbuffer.append(c)
            if c == 0x7F:
                # A copy of charbuffer
                pin_spec_list.append(charbuffer[:])
                charbuffer = []

        self._layout = pin_list_to_board_dict(pin_spec_list)
