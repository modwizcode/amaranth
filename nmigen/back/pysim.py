import os
import tempfile
import warnings
import inspect
from contextlib import contextmanager
import itertools
from vcd import VCDWriter
from vcd.gtkw import GTKWSave

from .._utils import deprecated
from ..hdl.ast import *
from ..hdl.cd import *
from ..hdl.ir import *
from ..hdl.xfrm import ValueVisitor, StatementVisitor, LHSGroupFilter


class Command:
    pass


class Settle(Command):
    def __repr__(self):
        return "(settle)"


class Delay(Command):
    def __init__(self, interval=None):
        self.interval = None if interval is None else float(interval)

    def __repr__(self):
        if self.interval is None:
            return "(delay ε)"
        else:
            return "(delay {:.3}us)".format(self.interval * 1e6)


class Tick(Command):
    def __init__(self, domain="sync"):
        if not isinstance(domain, (str, ClockDomain)):
            raise TypeError("Domain must be a string or a ClockDomain instance, not {!r}"
                            .format(domain))
        assert domain != "comb"
        self.domain = domain

    def __repr__(self):
        return "(tick {})".format(self.domain)


class Passive(Command):
    def __repr__(self):
        return "(passive)"


class Active(Command):
    def __repr__(self):
        return "(active)"


class _WaveformWriter:
    def update(self, timestamp, signal, value):
        raise NotImplementedError # :nocov:

    def close(self, timestamp):
        raise NotImplementedError # :nocov:


class _VCDWaveformWriter(_WaveformWriter):
    @staticmethod
    def timestamp_to_vcd(timestamp):
        return timestamp * (10 ** 10) # 1/(100 ps)

    @staticmethod
    def decode_to_vcd(signal, value):
        return signal.decoder(value).expandtabs().replace(" ", "_")

    def __init__(self, signal_names, *, vcd_file, gtkw_file=None, traces=()):
        if isinstance(vcd_file, str):
            vcd_file = open(vcd_file, "wt")
        if isinstance(gtkw_file, str):
            gtkw_file = open(gtkw_file, "wt")

        self.vcd_vars = SignalDict()
        self.vcd_file = vcd_file
        self.vcd_writer = vcd_file and VCDWriter(self.vcd_file,
            timescale="100 ps", comment="Generated by nMigen")

        self.gtkw_names = SignalDict()
        self.gtkw_file = gtkw_file
        self.gtkw_save = gtkw_file and GTKWSave(self.gtkw_file)

        self.traces = []

        trace_names = SignalDict()
        for trace in traces:
            if trace not in signal_names:
                trace_names[trace] = {("top", trace.name)}
            self.traces.append(trace)

        if self.vcd_writer is None:
            return

        for signal, names in itertools.chain(signal_names.items(), trace_names.items()):
            if signal.decoder:
                var_type = "string"
                var_size = 1
                var_init = self.decode_to_vcd(signal, signal.reset)
            else:
                var_type = "wire"
                var_size = signal.width
                var_init = signal.reset

            for (*var_scope, var_name) in names:
                suffix = None
                while True:
                    try:
                        if suffix is None:
                            var_name_suffix = var_name
                        else:
                            var_name_suffix = "{}${}".format(var_name, suffix)
                        vcd_var = self.vcd_writer.register_var(
                            scope=var_scope, name=var_name_suffix,
                            var_type=var_type, size=var_size, init=var_init)
                        break
                    except KeyError:
                        suffix = (suffix or 0) + 1

                if signal not in self.vcd_vars:
                    self.vcd_vars[signal] = set()
                self.vcd_vars[signal].add(vcd_var)

                if signal not in self.gtkw_names:
                    self.gtkw_names[signal] = (*var_scope, var_name_suffix)

    def update(self, timestamp, signal, value):
        vcd_vars = self.vcd_vars.get(signal)
        if vcd_vars is None:
            return

        vcd_timestamp = self.timestamp_to_vcd(timestamp)
        if signal.decoder:
            var_value = self.decode_to_vcd(signal, value)
        else:
            var_value = value
        for vcd_var in vcd_vars:
            self.vcd_writer.change(vcd_var, vcd_timestamp, var_value)

    def close(self, timestamp):
        if self.vcd_writer is not None:
            self.vcd_writer.close(self.timestamp_to_vcd(timestamp))

        if self.gtkw_save is not None:
            self.gtkw_save.dumpfile(self.vcd_file.name)
            self.gtkw_save.dumpfile_size(self.vcd_file.tell())

            self.gtkw_save.treeopen("top")
            for signal in self.traces:
                if len(signal) > 1 and not signal.decoder:
                    suffix = "[{}:0]".format(len(signal) - 1)
                else:
                    suffix = ""
                self.gtkw_save.trace(".".join(self.gtkw_names[signal]) + suffix)

        if self.vcd_file is not None:
            self.vcd_file.close()
        if self.gtkw_file is not None:
            self.gtkw_file.close()


class _Process:
    __slots__ = ("runnable", "passive")

    def reset(self):
        raise NotImplementedError # :nocov:

    def run(self):
        raise NotImplementedError # :nocov:

    @property
    def name(self):
        raise NotImplementedError # :nocov:


class _SignalState:
    __slots__ = ("signal", "curr", "next", "waiters", "pending")

    def __init__(self, signal, pending):
        self.signal = signal
        self.pending = pending
        self.waiters = dict()
        self.reset()

    def reset(self):
        self.curr = self.next = self.signal.reset

    def set(self, value):
        if self.next == value:
            return
        self.next = value
        self.pending.add(self)

    def wait(self, task, *, trigger=None):
        assert task not in self.waiters
        self.waiters[task] = trigger

    def commit(self):
        if self.curr == self.next:
            return False
        self.curr = self.next
        return True

    def wakeup(self):
        awoken_any = False
        for process, trigger in self.waiters.items():
            if trigger is None or trigger == self.curr:
                process.runnable = awoken_any = True
        return awoken_any


class _SimulatorState:
    def __init__(self):
        self.signals = SignalDict()
        self.slots   = []
        self.pending = set()

        self.timestamp = 0.0
        self.deadlines = dict()

        self.waveform_writer = None

    def reset(self):
        for signal, index in self.signals.items():
            self.slots[index].curr = self.slots[index].next = signal.reset
        self.pending.clear()

        self.timestamp = 0.0
        self.deadlines.clear()

    def get_signal(self, signal):
        try:
            return self.signals[signal]
        except KeyError:
            index = len(self.slots)
            self.slots.append(_SignalState(signal, self.pending))
            self.signals[signal] = index
            return index

    def get_in_signal(self, signal, *, trigger=None):
        index = self.get_signal(signal)
        self.slots[index].waiters[self] = trigger
        return index

    def get_out_signal(self, signal):
        return self.get_signal(signal)

    def for_signal(self, signal):
        return self.slots[self.get_signal(signal)]

    def commit(self):
        awoken_any = False
        for signal_state in self.pending:
            if signal_state.commit():
                if signal_state.wakeup():
                    awoken_any = True
                if self.waveform_writer is not None:
                    self.waveform_writer.update(self.timestamp,
                        signal_state.signal, signal_state.curr)
        self.pending.clear()
        return awoken_any

    def advance(self):
        nearest_processes = set()
        nearest_deadline = None
        for process, deadline in self.deadlines.items():
            if deadline is None:
                if nearest_deadline is not None:
                    nearest_processes.clear()
                nearest_processes.add(process)
                nearest_deadline = self.timestamp
                break
            elif nearest_deadline is None or deadline <= nearest_deadline:
                assert deadline >= self.timestamp
                if nearest_deadline is not None and deadline < nearest_deadline:
                    nearest_processes.clear()
                nearest_processes.add(process)
                nearest_deadline = deadline

        if not nearest_processes:
            return False

        for process in nearest_processes:
            process.runnable = True
            del self.deadlines[process]
        self.timestamp = nearest_deadline

        return True

    def start_waveform(self, waveform_writer):
        if self.timestamp != 0.0:
            raise ValueError("Cannot start writing waveforms after advancing simulation time")
        if self.waveform_writer is not None:
            raise ValueError("Already writing waveforms to {!r}"
                             .format(self.waveform_writer))
        self.waveform_writer = waveform_writer

    def finish_waveform(self):
        if self.waveform_writer is None:
            return
        self.waveform_writer.close(self.timestamp)
        self.waveform_writer = None


class _Emitter:
    def __init__(self):
        self._buffer = []
        self._suffix = 0
        self._level  = 0

    def append(self, code):
        self._buffer.append("    " * self._level)
        self._buffer.append(code)
        self._buffer.append("\n")

    @contextmanager
    def indent(self):
        self._level += 1
        yield
        self._level -= 1

    def flush(self, indent=""):
        code = "".join(self._buffer)
        self._buffer.clear()
        return code

    def gen_var(self, prefix):
        name = f"{prefix}_{self._suffix}"
        self._suffix += 1
        return name

    def def_var(self, prefix, value):
        name = self.gen_var(prefix)
        self.append(f"{name} = {value}")
        return name


class _Compiler:
    def __init__(self, state, emitter):
        self.state = state
        self.emitter = emitter


class _ValueCompiler(ValueVisitor, _Compiler):
    helpers = {
        "sign": lambda value, sign: value | sign if value & sign else value,
        "zdiv": lambda lhs, rhs: 0 if rhs == 0 else lhs // rhs,
        "zmod": lambda lhs, rhs: 0 if rhs == 0 else lhs % rhs,
    }

    def on_ClockSignal(self, value):
        raise NotImplementedError # :nocov:

    def on_ResetSignal(self, value):
        raise NotImplementedError # :nocov:

    def on_AnyConst(self, value):
        raise NotImplementedError # :nocov:

    def on_AnySeq(self, value):
        raise NotImplementedError # :nocov:

    def on_Sample(self, value):
        raise NotImplementedError # :nocov:

    def on_Initial(self, value):
        raise NotImplementedError # :nocov:


class _RHSValueCompiler(_ValueCompiler):
    def __init__(self, state, emitter, *, mode, inputs=None):
        super().__init__(state, emitter)
        assert mode in ("curr", "next")
        self.mode = mode
        # If not None, `inputs` gets populated with RHS signals.
        self.inputs = inputs

    def on_Const(self, value):
        return f"{value.value}"

    def on_Signal(self, value):
        if self.inputs is not None:
            self.inputs.add(value)

        if self.mode == "curr":
            return f"slots[{self.state.get_signal(value)}].{self.mode}"
        else:
            return f"next_{self.state.get_signal(value)}"

    def on_Operator(self, value):
        def mask(value):
            value_mask = (1 << len(value)) - 1
            return f"({self(value)} & {value_mask})"

        def sign(value):
            if value.shape().signed:
                return f"sign({mask(value)}, {-1 << (len(value) - 1)})"
            else: # unsigned
                return mask(value)

        if len(value.operands) == 1:
            arg, = value.operands
            if value.operator == "~":
                return f"(~{self(arg)})"
            if value.operator == "-":
                return f"(-{self(arg)})"
            if value.operator == "b":
                return f"bool({mask(arg)})"
            if value.operator == "r|":
                return f"({mask(arg)} != 0)"
            if value.operator == "r&":
                return f"({mask(arg)} == {(1 << len(arg)) - 1})"
            if value.operator == "r^":
                # Believe it or not, this is the fastest way to compute a sideways XOR in Python.
                return f"(format({mask(arg)}, 'b').count('1') % 2)"
            if value.operator in ("u", "s"):
                # These operators don't change the bit pattern, only its interpretation.
                return self(arg)
        elif len(value.operands) == 2:
            lhs, rhs = value.operands
            lhs_mask = (1 << len(lhs)) - 1
            rhs_mask = (1 << len(rhs)) - 1
            if value.operator == "+":
                return f"({sign(lhs)} + {sign(rhs)})"
            if value.operator == "-":
                return f"({sign(lhs)} - {sign(rhs)})"
            if value.operator == "*":
                return f"({sign(lhs)} * {sign(rhs)})"
            if value.operator == "//":
                return f"zdiv({sign(lhs)}, {sign(rhs)})"
            if value.operator == "%":
                return f"zmod({sign(lhs)}, {sign(rhs)})"
            if value.operator == "&":
                return f"({self(lhs)} & {self(rhs)})"
            if value.operator == "|":
                return f"({self(lhs)} | {self(rhs)})"
            if value.operator == "^":
                return f"({self(lhs)} ^ {self(rhs)})"
            if value.operator == "<<":
                return f"({sign(lhs)} << {sign(rhs)})"
            if value.operator == ">>":
                return f"({sign(lhs)} >> {sign(rhs)})"
            if value.operator == "==":
                return f"({sign(lhs)} == {sign(rhs)})"
            if value.operator == "!=":
                return f"({sign(lhs)} != {sign(rhs)})"
            if value.operator == "<":
                return f"({sign(lhs)} < {sign(rhs)})"
            if value.operator == "<=":
                return f"({sign(lhs)} <= {sign(rhs)})"
            if value.operator == ">":
                return f"({sign(lhs)} > {sign(rhs)})"
            if value.operator == ">=":
                return f"({sign(lhs)} >= {sign(rhs)})"
        elif len(value.operands) == 3:
            if value.operator == "m":
                sel, val1, val0 = value.operands
                return f"({self(val1)} if {self(sel)} else {self(val0)})"
        raise NotImplementedError("Operator '{}' not implemented".format(value.operator)) # :nocov:

    def on_Slice(self, value):
        return f"(({self(value.value)} >> {value.start}) & {(1 << len(value)) - 1})"

    def on_Part(self, value):
        offset_mask = (1 << len(value.offset)) - 1
        offset = f"(({self(value.offset)} & {offset_mask}) * {value.stride})"
        return f"({self(value.value)} >> {offset} & " \
               f"{(1 << value.width) - 1})"

    def on_Cat(self, value):
        gen_parts = []
        offset = 0
        for part in value.parts:
            part_mask = (1 << len(part)) - 1
            gen_parts.append(f"(({self(part)} & {part_mask}) << {offset})")
            offset += len(part)
        if gen_parts:
            return f"({' | '.join(gen_parts)})"
        return f"0"

    def on_Repl(self, value):
        part_mask = (1 << len(value.value)) - 1
        gen_part = self.emitter.def_var("repl", f"{self(value.value)} & {part_mask}")
        gen_parts = []
        offset = 0
        for _ in range(value.count):
            gen_parts.append(f"({gen_part} << {offset})")
            offset += len(value.value)
        if gen_parts:
            return f"({' | '.join(gen_parts)})"
        return f"0"

    def on_ArrayProxy(self, value):
        index_mask = (1 << len(value.index)) - 1
        gen_index = self.emitter.def_var("rhs_index", f"{self(value.index)} & {index_mask}")
        gen_value = self.emitter.gen_var("rhs_proxy")
        if value.elems:
            gen_elems = []
            for index, elem in enumerate(value.elems):
                if index == 0:
                    self.emitter.append(f"if {gen_index} == {index}:")
                else:
                    self.emitter.append(f"elif {gen_index} == {index}:")
                with self.emitter.indent():
                    self.emitter.append(f"{gen_value} = {self(elem)}")
            self.emitter.append(f"else:")
            with self.emitter.indent():
                self.emitter.append(f"{gen_value} = {self(value.elems[-1])}")
            return gen_value
        else:
            return f"0"

    @classmethod
    def compile(cls, state, value, *, mode, inputs=None):
        emitter = _Emitter()
        compiler = cls(state, emitter, mode=mode, inputs=inputs)
        emitter.append(f"result = {compiler(value)}")
        return emitter.flush()


class _LHSValueCompiler(_ValueCompiler):
    def __init__(self, state, emitter, *, rhs, outputs=None):
        super().__init__(state, emitter)
        # `rrhs` is used to translate rvalues that are syntactically a part of an lvalue, e.g.
        # the offset of a Part.
        self.rrhs = rhs
        # `lrhs` is used to translate the read part of a read-modify-write cycle during partial
        # update of an lvalue.
        self.lrhs = _RHSValueCompiler(state, emitter, mode="next", inputs=None)
        # If not None, `outputs` gets populated with signals on LHS.
        self.outputs = outputs

    def on_Const(self, value):
        raise TypeError # :nocov:

    def on_Signal(self, value):
        if self.outputs is not None:
            self.outputs.add(value)

        def gen(arg):
            value_mask = (1 << len(value)) - 1
            if value.shape().signed:
                value_sign = f"sign({arg} & {value_mask}, {-1 << (len(value) - 1)})"
            else: # unsigned
                value_sign = f"{arg} & {value_mask}"
            self.emitter.append(f"next_{self.state.get_out_signal(value)} = {value_sign}")
        return gen

    def on_Operator(self, value):
        raise TypeError # :nocov:

    def on_Slice(self, value):
        def gen(arg):
            width_mask = (1 << (value.stop - value.start)) - 1
            self(value.value)(f"({self.lrhs(value.value)} & " \
                f"{~(width_mask << value.start)} | " \
                f"(({arg} & {width_mask}) << {value.start}))")
        return gen

    def on_Part(self, value):
        def gen(arg):
            width_mask = (1 << value.width) - 1
            offset_mask = (1 << len(value.offset)) - 1
            offset = f"(({self.rrhs(value.offset)} & {offset_mask}) * {value.stride})"
            self(value.value)(f"({self.lrhs(value.value)} & " \
                f"~({width_mask} << {offset}) | " \
                f"(({arg} & {width_mask}) << {offset}))")
        return gen

    def on_Cat(self, value):
        def gen(arg):
            gen_arg = self.emitter.def_var("cat", arg)
            gen_parts = []
            offset = 0
            for part in value.parts:
                part_mask = (1 << len(part)) - 1
                self(part)(f"(({gen_arg} >> {offset}) & {part_mask})")
                offset += len(part)
        return gen

    def on_Repl(self, value):
        raise TypeError # :nocov:

    def on_ArrayProxy(self, value):
        def gen(arg):
            index_mask = (1 << len(value.index)) - 1
            gen_index = self.emitter.def_var("index", f"{self.rrhs(value.index)} & {index_mask}")
            if value.elems:
                gen_elems = []
                for index, elem in enumerate(value.elems):
                    if index == 0:
                        self.emitter.append(f"if {gen_index} == {index}:")
                    else:
                        self.emitter.append(f"elif {gen_index} == {index}:")
                    with self.emitter.indent():
                        self(elem)(arg)
                self.emitter.append(f"else:")
                with self.emitter.indent():
                    self(value.elems[-1])(arg)
            else:
                self.emitter.append(f"pass")
        return gen

    @classmethod
    def compile(cls, state, stmt, *, inputs=None, outputs=None):
        emitter = _Emitter()
        compiler = cls(state, emitter, inputs=inputs, outputs=outputs)
        compiler(stmt)
        return emitter.flush()


class _StatementCompiler(StatementVisitor, _Compiler):
    def __init__(self, state, emitter, *, inputs=None, outputs=None):
        super().__init__(state, emitter)
        self.rhs = _RHSValueCompiler(state, emitter, mode="curr", inputs=inputs)
        self.lhs = _LHSValueCompiler(state, emitter, rhs=self.rhs, outputs=outputs)

    def on_statements(self, stmts):
        for stmt in stmts:
            self(stmt)
        if not stmts:
            self.emitter.append("pass")

    def on_Assign(self, stmt):
        return self.lhs(stmt.lhs)(self.rhs(stmt.rhs))

    def on_Switch(self, stmt):
        gen_test = self.emitter.def_var("test",
            f"{self.rhs(stmt.test)} & {(1 << len(stmt.test)) - 1}")
        for index, (patterns, stmts) in enumerate(stmt.cases.items()):
            gen_checks = []
            if not patterns:
                gen_checks.append(f"True")
            else:
                for pattern in patterns:
                    if "-" in pattern:
                        mask  = int("".join("0" if b == "-" else "1" for b in pattern), 2)
                        value = int("".join("0" if b == "-" else  b  for b in pattern), 2)
                        gen_checks.append(f"({gen_test} & {mask}) == {value}")
                    else:
                        value = int(pattern, 2)
                        gen_checks.append(f"{gen_test} == {value}")
            if index == 0:
                self.emitter.append(f"if {' or '.join(gen_checks)}:")
            else:
                self.emitter.append(f"elif {' or '.join(gen_checks)}:")
            with self.emitter.indent():
                self(stmts)

    def on_Assert(self, stmt):
        raise NotImplementedError # :nocov:

    def on_Assume(self, stmt):
        raise NotImplementedError # :nocov:

    def on_Cover(self, stmt):
        raise NotImplementedError # :nocov:

    @classmethod
    def compile(cls, state, stmt, *, inputs=None, outputs=None):
        output_indexes = [state.get_signal(signal) for signal in stmt._lhs_signals()]
        emitter = _Emitter()
        for signal_index in output_indexes:
            emitter.append(f"next_{signal_index} = slots[{signal_index}].next")
        compiler = cls(state, emitter, inputs=inputs, outputs=outputs)
        compiler(stmt)
        for signal_index in output_indexes:
            emitter.append(f"slots[{signal_index}].set(next_{signal_index})")
        return emitter.flush()


class _CompiledProcess(_Process):
    __slots__ = ("state", "comb", "name", "run")

    def __init__(self, state, *, comb, name):
        self.state = state
        self.comb = comb
        self.name = name
        self.run = None # set by _FragmentCompiler
        self.reset()

    def reset(self):
        self.runnable = self.comb
        self.passive = True


class _FragmentCompiler:
    def __init__(self, state, signal_names):
        self.state = state
        self.signal_names = signal_names

    def __call__(self, fragment, *, hierarchy=("top",)):
        processes = set()

        def add_signal_name(signal):
            hierarchical_signal_name = (*hierarchy, signal.name)
            if signal not in self.signal_names:
                self.signal_names[signal] = {hierarchical_signal_name}
            else:
                self.signal_names[signal].add(hierarchical_signal_name)

        for domain_name, domain_signals in fragment.drivers.items():
            domain_stmts = LHSGroupFilter(domain_signals)(fragment.statements)
            domain_process = _CompiledProcess(self.state, comb=domain_name is None,
                name=".".join((*hierarchy, "<{}>".format(domain_name or "comb"))))

            emitter = _Emitter()
            emitter.append(f"def run():")
            emitter._level += 1

            if domain_name is None:
                for signal in domain_signals:
                    signal_index = domain_process.state.get_signal(signal)
                    emitter.append(f"next_{signal_index} = {signal.reset}")

                inputs = SignalSet()
                _StatementCompiler(domain_process.state, emitter, inputs=inputs)(domain_stmts)

                for input in inputs:
                    self.state.for_signal(input).wait(domain_process)

            else:
                domain = fragment.domains[domain_name]
                add_signal_name(domain.clk)
                if domain.rst is not None:
                    add_signal_name(domain.rst)

                clk_trigger = 1 if domain.clk_edge == "pos" else 0
                self.state.for_signal(domain.clk).wait(domain_process, trigger=clk_trigger)
                if domain.rst is not None and domain.async_reset:
                    rst_trigger = 1
                    self.state.for_signal(domain.rst).wait(domain_process, trigger=rst_trigger)

                gen_asserts = []
                clk_index = domain_process.state.get_signal(domain.clk)
                gen_asserts.append(f"slots[{clk_index}].curr == {clk_trigger}")
                if domain.rst is not None and domain.async_reset:
                    rst_index = domain_process.state.get_signal(domain.rst)
                    gen_asserts.append(f"slots[{rst_index}].curr == {rst_trigger}")
                emitter.append(f"assert {' or '.join(gen_asserts)}")

                for signal in domain_signals:
                    signal_index = domain_process.state.get_signal(signal)
                    emitter.append(f"next_{signal_index} = slots[{signal_index}].next")

                _StatementCompiler(domain_process.state, emitter)(domain_stmts)

            for signal in domain_signals:
                signal_index = domain_process.state.get_signal(signal)
                emitter.append(f"slots[{signal_index}].set(next_{signal_index})")

            # There shouldn't be any exceptions raised by the generated code, but if there are
            # (almost certainly due to a bug in the code generator), use this environment variable
            # to make backtraces useful.
            code = emitter.flush()
            if os.getenv("NMIGEN_pysim_dump"):
                file = tempfile.NamedTemporaryFile("w", prefix="nmigen_pysim_", delete=False)
                file.write(code)
                filename = file.name
            else:
                filename = "<string>"

            exec_locals = {"slots": domain_process.state.slots, **_ValueCompiler.helpers}
            exec(compile(code, filename, "exec"), exec_locals)
            domain_process.run = exec_locals["run"]

            processes.add(domain_process)

            for used_signal in domain_process.state.signals:
                add_signal_name(used_signal)

        for subfragment_index, (subfragment, subfragment_name) in enumerate(fragment.subfragments):
            if subfragment_name is None:
                subfragment_name = "U${}".format(subfragment_index)
            processes.update(self(subfragment, hierarchy=(*hierarchy, subfragment_name)))

        return processes


class _CoroutineProcess(_Process):
    def __init__(self, state, domains, constructor, *, default_cmd=None):
        self.state = state
        self.domains = domains
        self.constructor = constructor
        self.default_cmd = default_cmd
        self.reset()

    def reset(self):
        self.runnable = True
        self.passive = False
        self.coroutine = self.constructor()
        self.exec_locals = {
            "slots": self.state.slots,
            "result": None,
            **_ValueCompiler.helpers
        }
        self.waits_on = set()

    @property
    def name(self):
        coroutine = self.coroutine
        while coroutine.gi_yieldfrom is not None:
            coroutine = coroutine.gi_yieldfrom
        if inspect.isgenerator(coroutine):
            frame = coroutine.gi_frame
        if inspect.iscoroutine(coroutine):
            frame = coroutine.cr_frame
        return "{}:{}".format(inspect.getfile(frame), inspect.getlineno(frame))

    def get_in_signal(self, signal, *, trigger=None):
        signal_state = self.state.for_signal(signal)
        assert self not in signal_state.waiters
        signal_state.waiters[self] = trigger
        self.waits_on.add(signal_state)
        return signal_state

    def run(self):
        if self.coroutine is None:
            return

        if self.waits_on:
            for signal_state in self.waits_on:
                del signal_state.waiters[self]
            self.waits_on.clear()

        response = None
        while True:
            try:
                command = self.coroutine.send(response)
                if command is None:
                    command = self.default_cmd
                response = None

                if isinstance(command, Value):
                    exec(_RHSValueCompiler.compile(self.state, command, mode="curr"),
                        self.exec_locals)
                    response = Const.normalize(self.exec_locals["result"], command.shape())

                elif isinstance(command, Statement):
                    exec(_StatementCompiler.compile(self.state, command),
                        self.exec_locals)

                elif type(command) is Tick:
                    domain = command.domain
                    if isinstance(domain, ClockDomain):
                        pass
                    elif domain in self.domains:
                        domain = self.domains[domain]
                    else:
                        raise NameError("Received command {!r} that refers to a nonexistent "
                                        "domain {!r} from process {!r}"
                                        .format(command, command.domain, self.name))
                    self.get_in_signal(domain.clk, trigger=1 if domain.clk_edge == "pos" else 0)
                    if domain.rst is not None and domain.async_reset:
                        self.get_in_signal(domain.rst, trigger=1)
                    return

                elif type(command) is Settle:
                    self.state.deadlines[self] = None
                    return

                elif type(command) is Delay:
                    if command.interval is None:
                        self.state.deadlines[self] = None
                    else:
                        self.state.deadlines[self] = self.state.timestamp + command.interval
                    return

                elif type(command) is Passive:
                    self.passive = True

                elif type(command) is Active:
                    self.passive = False

                elif command is None: # only possible if self.default_cmd is None
                    raise TypeError("Received default command from process {!r} that was added "
                                    "with add_process(); did you mean to add this process with "
                                    "add_sync_process() instead?"
                                    .format(self.name))

                else:
                    raise TypeError("Received unsupported command {!r} from process {!r}"
                                    .format(command, self.name))

            except StopIteration:
                self.passive = True
                self.coroutine = None
                return

            except Exception as exn:
                self.coroutine.throw(exn)


class _WaveformContextManager:
    def __init__(self, state, waveform_writer):
        self._state = state
        self._waveform_writer = waveform_writer

    def __enter__(self):
        try:
            self._state.start_waveform(self._waveform_writer)
        except:
            self._waveform_writer.close(0)
            raise

    def __exit__(self, *args):
        self._state.finish_waveform()


class Simulator:
    def __init__(self, fragment):
        self._state = _SimulatorState()
        self._signal_names = SignalDict()
        self._fragment = Fragment.get(fragment, platform=None).prepare()
        self._processes = _FragmentCompiler(self._state, self._signal_names)(self._fragment)
        self._clocked = set()

    def _check_process(self, process):
        if not (inspect.isgeneratorfunction(process) or inspect.iscoroutinefunction(process)):
            raise TypeError("Cannot add a process {!r} because it is not a generator function"
                            .format(process))
        return process

    def _add_coroutine_process(self, process, *, default_cmd):
        self._processes.add(_CoroutineProcess(self._state, self._fragment.domains, process,
                                              default_cmd=default_cmd))

    def add_process(self, process):
        process = self._check_process(process)
        def wrapper():
            # Only start a bench process after comb settling, so that the reset values are correct.
            yield Settle()
            yield from process()
        self._add_coroutine_process(wrapper, default_cmd=None)

    def add_sync_process(self, process, *, domain="sync"):
        process = self._check_process(process)
        def wrapper():
            # Only start a sync process after the first clock edge (or reset edge, if the domain
            # uses an asynchronous reset). This matches the behavior of synchronous FFs.
            yield Tick(domain)
            yield from process()
        return self._add_coroutine_process(wrapper, default_cmd=Tick(domain))

    def add_clock(self, period, *, phase=None, domain="sync", if_exists=False):
        """Add a clock process.

        Adds a process that drives the clock signal of ``domain`` at a 50% duty cycle.

        Arguments
        ---------
        period : float
            Clock period. The process will toggle the ``domain`` clock signal every ``period / 2``
            seconds.
        phase : None or float
            Clock phase. The process will wait ``phase`` seconds before the first clock transition.
            If not specified, defaults to ``period / 2``.
        domain : str or ClockDomain
            Driven clock domain. If specified as a string, the domain with that name is looked up
            in the root fragment of the simulation.
        if_exists : bool
            If ``False`` (the default), raise an error if the driven domain is specified as
            a string and the root fragment does not have such a domain. If ``True``, do nothing
            in this case.
        """
        if isinstance(domain, ClockDomain):
            pass
        elif domain in self._fragment.domains:
            domain = self._fragment.domains[domain]
        elif if_exists:
            return
        else:
            raise ValueError("Domain {!r} is not present in simulation"
                             .format(domain))
        if domain in self._clocked:
            raise ValueError("Domain {!r} already has a clock driving it"
                             .format(domain.name))

        half_period = period / 2
        if phase is None:
            # By default, delay the first edge by half period. This causes any synchronous activity
            # to happen at a non-zero time, distinguishing it from the reset values in the waveform
            # viewer.
            phase = half_period
        def clk_process():
            yield Passive()
            yield Delay(phase)
            # Behave correctly if the process is added after the clock signal is manipulated, or if
            # its reset state is high.
            initial = (yield domain.clk)
            steps = (
                domain.clk.eq(~initial),
                Delay(half_period),
                domain.clk.eq(initial),
                Delay(half_period),
            )
            while True:
                yield from iter(steps)
        self._add_coroutine_process(clk_process, default_cmd=None)
        self._clocked.add(domain)

    def reset(self):
        """Reset the simulation.

        Assign the reset value to every signal in the simulation, and restart every user process.
        """
        self._state.reset()
        for process in self._processes:
            process.reset()

    def _delta(self):
        """Perform a delta cycle.

        Performs the two phases of a delta cycle:
            1. run and suspend every non-waiting process once, queueing signal changes;
            2. commit every queued signal change, waking up any waiting process.
        """
        for process in self._processes:
            if process.runnable:
                process.runnable = False
                process.run()

        return self._state.commit()

    def _settle(self):
        """Settle the simulation.

        Run every process and commit changes until a fixed point is reached. If there is
        an unstable combinatorial loop, this function will never return.
        """
        while self._delta():
            pass

    def step(self):
        """Step the simulation.

        Run every process and commit changes until a fixed point is reached, then advance time
        to the closest deadline (if any). If there is an unstable combinatorial loop,
        this function will never return.

        Returns ``True`` if there are any active processes, ``False`` otherwise.
        """
        self._settle()
        self._state.advance()
        return any(not process.passive for process in self._processes)

    def run(self):
        """Run the simulation while any processes are active.

        Processes added with :meth:`add_process` and :meth:`add_sync_process` are initially active,
        and may change their status using the ``yield Passive()`` and ``yield Active()`` commands.
        Processes compiled from HDL and added with :meth:`add_clock` are always passive.
        """
        while self.step():
            pass

    def run_until(self, deadline, *, run_passive=False):
        """Run the simulation until it advances to ``deadline``.

        If ``run_passive`` is ``False``, the simulation also stops when there are no active
        processes, similar to :meth:`run`. Otherwise, the simulation will stop only after it
        advances to or past ``deadline``.

        If the simulation stops advancing, this function will never return.
        """
        assert self._state.timestamp <= deadline
        while (self.step() or run_passive) and self._state.timestamp < deadline:
            pass

    def write_vcd(self, vcd_file, gtkw_file=None, *, traces=()):
        """Write waveforms to a Value Change Dump file, optionally populating a GTKWave save file.

        This method returns a context manager. It can be used as: ::

            sim = Simulator(frag)
            sim.add_clock(1e-6)
            with sim.write_vcd("dump.vcd", "dump.gtkw"):
                sim.run_until(1e-3)

        Arguments
        ---------
        vcd_file : str or file-like object
            Verilog Value Change Dump file or filename.
        gtkw_file : str or file-like object
            GTKWave save file or filename.
        traces : iterable of Signal
            Signals to display traces for.
        """
        waveform_writer = _VCDWaveformWriter(self._signal_names,
            vcd_file=vcd_file, gtkw_file=gtkw_file, traces=traces)
        return _WaveformContextManager(self._state, waveform_writer)
