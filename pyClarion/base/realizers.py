"""Tools for networking constructs and defining construct behavior."""


__all__ = ["Realizer", "Construct", "Structure"]


from .symbols import ConstructType, Symbol, SymbolicAddress, SymbolTrie, feature
from .components import (
    Emitter, Propagator, Updater, UpdaterC, UpdaterS, Cycle, Assets
)
from ..numdicts import NumDict

from itertools import combinations, chain
from abc import abstractmethod
from types import MappingProxyType
from typing import (
    TypeVar, Union, Tuple, Dict, Callable, Hashable, Generic, Any, Optional, 
    Text, Iterator, Iterable, Mapping, ClassVar, List, ContextManager, cast
)
from contextlib import nullcontext
from contextvars import ContextVar
import logging


Et = TypeVar("Et", bound="Emitter")
Pt = TypeVar("Pt", bound="Propagator")
Ut = TypeVar("Ut", bound="Updater")
Ct = TypeVar("Ct", bound="Cycle")
Ot = TypeVar("Ot", bound=Union[NumDict, SymbolTrie[NumDict]])

PullFunc = Union[Callable[[], NumDict], Callable[[], SymbolTrie[NumDict]]]
PullFuncs = Mapping[Symbol, PullFunc]
StructureItem = Tuple[Symbol, "Realizer"]


# Context variables for automating/simplifying agent construction. Helps track
# items to be added to structures. 
BUILD_CTX: ContextVar[Tuple[Symbol, ...]] = ContextVar("BUILD_CTX")
BUILD_LIST: ContextVar[List["Realizer"]] = ContextVar("BUILD_LIST")


class RealizerError(Exception):
    """Thrown when a realizer is found not to be properly initialized."""
    pass


class Realizer(Generic[Et, Ut, Ot]):
    """
    Base class for construct realizers.

    Provides a standard interface for creating, inspecting, modifying and 
    propagating information across construct networks. 

    Follows a pull-based message-passing pattern for activation propagation. 
    """

    _inputs: Dict[Symbol, PullFunc]
    _output: Ot
    _emitter: Et
    _updater: Optional[Ut]
    _input_cache: SymbolTrie[NumDict]
    _update_cache: SymbolTrie[NumDict]

    def __init__(self, name: Symbol, emitter: Et, updater: Ut = None) -> None:
        """
        Initialize a new Realizer instance.
        
        :param name: Identifier for client construct.  
        :param emitter: Procedure for activation propagation. Expected to be of 
            type Emitter.
        :param updater: Procedure for updating persistent construct data. 
            Expected to be of type Updater.
        """

        self._validate_name(name)
        self._log_init(name)

        self._construct = name
        self._inputs = {}
        self._output = emitter.emit()
        self._input_cache = {}
        self._update_cache = {}

        self.emitter = emitter
        self.updater = updater

        self._update_add_queue()

    def __repr__(self) -> Text:

        return "<{}: {}>".format(self.__class__.__name__, str(self.construct))

    @property
    def construct(self) -> Symbol:
        """Symbol for client construct."""

        return self._construct

    @property
    def emitter(self) -> Et:
        """Emitter for client construct."""

        return self._emitter

    @emitter.setter
    def emitter(self, emitter: Et) -> None:

        emitter.entrust(self.construct)
        self._emitter = emitter

    @property
    def updater(self) -> Optional[Ut]:
        """Updater for client construct."""

        return self._updater

    @updater.setter
    def updater(self, updater: Optional[Ut]) -> None:

        if updater is not None:
            updater.entrust(self.construct)
        self._updater = updater

    @property 
    def inputs(self) -> Mapping[Symbol, PullFunc]:
        """Mapping from input constructs to pull funcs."""

        return MappingProxyType(self._inputs)

    @property
    def output(self) -> Ot:
        """
        Current output of self.
        
        Deleteing this attribute will simply revert it to the default value.
        """

        return self._output

    @output.setter
    def output(self, output: Ot) -> None:

        self._output = output

    @output.deleter
    def output(self) -> None:
        
        self._output = self.emitter.emit() # default/empty output

    def step(self) -> None:
        """Advance the simulation by one time step."""

        self._propagate()
        self._update()

    def view(self) -> Ot:
        """Return current output of self."""
        
        return self._output

    def _accepts(self, source: Symbol) -> bool:
        """Return true iff self pulls information from source."""

        val = self.emitter.expects(source)
        if self.updater is not None:
            val |= self.updater.expects(source)

        return val

    def _offer(self, construct: Symbol, callback: PullFunc) -> None:
        """
        Add link from construct to self if self accepts construct.
        
        :param construct: Symbol for target construct.
        :param callback: A callable that returns data representing the output 
            of construct. Typically this will be the `view()` method of a 
            Realizer instance.
        """

        if self._accepts(construct):
            self._log_watch(construct)            
            self._inputs[construct] = callback

    def _finalize_assembly(self):
        """Execute final initialization and checks prior to simulation."""

        b = self.emitter.check_links(self.inputs)
        b &= self.updater is None or self.updater.check_links(self.inputs)
        
        if not b:
            raise RealizerError(self._link_error_msg())

    @abstractmethod
    def _propagate(self) -> None:
        """
        Propagate activations.

        :param kwds: Keyword arguments for emitter.
        """

        raise NotImplementedError()

    @abstractmethod
    def _update(self) -> None:
        """Update persistent data associated with self."""
        
        raise NotImplementedError()

    def _pull_input_data(self) -> None:

        items = self.inputs.items()
        data = {src: ask() for src, ask in items if self.emitter.expects(src)}
        self._input_cache = MappingProxyType(data)

    def _pull_update_data(self) -> None:

        if self.updater is not None:
            items = self.inputs.items()
            condition = self.updater.expects
            data = {src: ask() for src, ask in items if condition(src)}
            self._update_cache = MappingProxyType(data)

    def _update_add_queue(self) -> None:
        """If current context contains an add queue, add self to it."""

        try:
            lst = BUILD_LIST.get()
        except LookupError:
            pass
        else:
            lst.append(self)

    def _log_init(self, construct) -> None:

        tname = type(self).__name__
        try:
            context = BUILD_CTX.get()
        except LookupError:
            msg = "Initializing %s %s."
            logging.debug(msg, tname, construct)
        else:
            msg = "Initializing %s %s in %s."
            logging.debug(msg, tname, construct, context)

    def _log_watch(self, construct: Symbol) -> None:

        try:
            context = BUILD_CTX.get()
        except LookupError:
            logging.debug("Connecting %s to %s.", construct, self.construct)
        else:
            msg = "Connecting %s to %s in %s."
            logging.debug(msg, construct, self.construct, context)

    def _link_error_msg(self):

        s = set(self.emitter.expected)
        s |= self.updater.expected if self.updater is not None else set()
        s -= set(self.inputs)

        try:
            context = BUILD_CTX.get()
        except LookupError:
            msg = "Construct {} missing expected link(s): {}."
            return msg.format(self.construct, s)
        else:
            msg = "Construct {} in {} missing expected link(s): {}."
            return msg.format(self.construct, context, s)

    @staticmethod
    def _validate_name(name) -> None:

        if not isinstance(name, Symbol):
            msg = "Agrument 'name' must be of type Symbol got '{}' instead."
            raise TypeError(msg.format(type(name).__name__))


class Construct(Realizer[Pt, UpdaterC[Pt], NumDict]):
    """
    A basic construct.
    
    Responsible for defining the behaviour of lowest-level constructs such as 
    individual nodes, bottom level networks, top level rule databases, 
    subsystem output terminals, short term memory buffers and so on.
    """

    def __init__(
        self,
        name: Symbol,
        emitter: Pt,
        updater: UpdaterC[Pt] = None,
    ) -> None:
        """
        Initialize a new construct realizer.
        
        :param name: Identifier for client construct.  
        :param emitter: Procedure for activation propagation. Expected to be of 
            type Propagator.
        :param updater: Procedure for updating persistent construct data.
        """

        super().__init__(name=name, emitter=emitter, updater=updater)

    def _propagate(self) -> None:

        self._pull_input_data()
        self.output = self.emitter(self._input_cache)

    def _update(self) -> None:

        self.emitter.update(self._input_cache, self.output)
        self._pull_update_data()
        if self.updater is not None:
            updater = cast(UpdaterC[Pt], self.updater)
            updater( 
                propagator=self.emitter, 
                inputs=self._input_cache, 
                output=self.output, 
                update_data=self._update_cache
            )

        
class Structure(Realizer[Ct, UpdaterS, SymbolTrie[NumDict]]):
    """
    A composite construct.
    
    Defines behaviour of higher-level constructs, such as agents and subsystems,
    which may contain other constructs. 

    Any Realizer initialized within the body of a with statement having a 
    Structure object as its context manager will automatically be added to the 
    Structure upon exit from the context. Nested use of with statements in this 
    way (e.g. to add objects to subsystems within an agent) is well-behaved.
    """

    _dict: Dict[ConstructType, Dict[Symbol, Realizer]]
    _assets: Any

    def __init__(
        self, 
        name: Symbol, 
        emitter: Ct,
        assets: Any = None,
        updater: UpdaterS = None,
    ) -> None:
        """
        Initialize a new Structure instance.
        
        :param name: Identifier for client construct.  
        :param emitter: Procedure for activation propagation. Expected to be of 
            type Emitter.
        :param assets: Data structure storing persistent data shared among 
            members of self.
        :param updater: Procedure for updating persistent construct data.
        """

        super().__init__(name=name, emitter=emitter, updater=updater)
        
        self._dict = {}
        self.assets = assets if assets is not None else Assets()

    def __contains__(self, key: SymbolicAddress) -> bool:

        try:
            self.__getitem__(key)
        except KeyError:
            return False
        return True

    def __iter__(self) -> Iterator[Symbol]:

        for construct in chain(*self._dict.values()):
            yield construct

    def __getitem__(self, key: SymbolicAddress) -> Any:

        if isinstance(key, tuple):
            if len(key) == 0:
                raise KeyError("Key sequence must be of length 1 at least.")
            elif len(key) == 1:
                return self[key[0]]
            else:
                # Catch & output more informative error here? - Can
                head = self[key[0]]
                return head[key[1:]] 
        else:
            return self._dict[key.ctype][key]

    def __enter__(self):

        logging.debug("Entering context %s.", self.construct)
        # This sets the context variable up to track objects to be added to 
        # self.
        parent = BUILD_CTX.get(())
        self._build_ctx_token = BUILD_CTX.set(parent + (self.construct,))
        self._build_list_token = BUILD_LIST.set([])

    def __exit__(self, exc_type, exc_value, traceback):

        if exc_type is None:
            # Add any newly defined realizers to self and clean up the context.
            context, add_list = BUILD_CTX.get(), BUILD_LIST.get()
            self._add(*add_list)
            if len(context) <= 1:
                self._finalize_assembly()
        print(add_list)
        BUILD_CTX.reset(self._build_ctx_token)
        BUILD_LIST.reset(self._build_list_token)
        logging.debug("Exiting context %s.", self.construct)

    def keys(self, ctype: ConstructType = None) -> Iterator[Symbol]:
        """
        Return iterator over all construct symbols in self.
        
        :param ctype: If provided, only constructs of a type that have a 
            non-empty intersection with ctype will be included.
        """

        for ct in self._dict:
            if ctype is None or bool(ct & ctype):
                for construct in self._dict[ct]:
                    yield construct

    def values(self, ctype: ConstructType = None) -> Iterator[Realizer]:
        """
        Return iterator over all construct realizers in self.
        
        :param ctype: If provided, only constructs of a type that have a 
            non-empty intersection with ctype will be included.
        """

        for ct in self._dict:
            if ctype is None or bool(ct & ctype):
                for realizer in self._dict[ct].values():
                    yield realizer

    def items(self, ctype: ConstructType = None) -> Iterator[StructureItem]:
        """
        Return iterator over all symbol, realizer pairs in self.
        
        :param ctype: If provided, only constructs of a type that have a 
            non-empty intersection with ctype will be included.
        """

        for ct in self._dict:
            if ctype is None or bool(ct & ctype):
                for construct, realizer in self._dict[ct].items():
                    yield construct, realizer

    def reset_output(self) -> None:
        """Set output of self to reflect member outputs."""

        ctype = self.emitter.output
        data = {sym: c.output for sym, c in self.items(ctype=ctype)}
        self.output = self.emitter.emit(data)

    def clear_outputs(self) -> None:
        """Clear output of self and all members."""

        for realizer in self.values():
            if isinstance(realizer, Structure):
                realizer.clear_outputs()
            else:
                del realizer.output

        self.reset_output()

    def _add(self, *realizers: Realizer) -> None:
        """Add realizers to self and any associated links."""

        for realizer in realizers:
            self._log_add(realizer.construct)
            ctype = realizer.construct.ctype
            d = self._dict.setdefault(ctype, {})
            d[realizer.construct] = realizer
            self._update_links(construct=realizer.construct)

    def _update_links(self, construct: Symbol) -> None:
        """Add links between member construct and any other member of self."""

        target = self[construct]
        for realizer in self.values():
            if target.construct != realizer.construct:
                realizer._offer(target.construct, target.view)
                target._offer(realizer.construct, realizer.view)

    def _offer(self, construct: Symbol, callback: PullFunc) -> None:
        """
        Add links from construct to self and any accepting members.
        
        :param construct: Symbol for target construct.
        :param callback: A callable that returns data representing the output 
            of construct. Typically this will be the `view()` method of a 
            Realizer instance.
        """

        super()._offer(construct, callback)  
        for realizer in self.values():
            realizer._offer(construct, callback)

    def _propagate(self) -> None:

        for ctype in self.emitter.sequence:
            for c in self.values(ctype=ctype):
                c._propagate()
        self.reset_output()

    def _finalize_assembly(self) -> None:

        for realizer in self.values():
            if isinstance(realizer, Structure):
                realizer._finalize_assembly()
        for realizer in self.values():
            if isinstance(realizer, Construct):
                realizer._finalize_assembly()
        self.reset_output()
        super()._finalize_assembly()

    def _update(self) -> None:
        """
        Update persistent data in self and all members.
        
        Subordinate constructs are prioritized in the update order. Further, 
        Structures are prioritized over Constructs in the ordering. In other 
        words, updates are applied in a roughly bottom-up manner relative to 
        the construct hierarchy. There are otherwise no guarantees as to the 
        ordering of updates.
        """

        self._pull_update_data()

        for realizer in self.values():
            if isinstance(realizer, Structure):
                realizer._update()
        for realizer in self.values():
            if isinstance(realizer, Construct):
                realizer._update()

        if self.updater is not None:
            updater = cast(UpdaterS, self.updater)
            updater(
                inputs=self._input_cache,
                output=self.output,
                update_data=self._update_cache
            )

    def _log_add(self, construct) -> None:

        try:
            context = BUILD_CTX.get()
        except LookupError:
            logging.debug("Adding %s to %s.", construct, self.construct)
        else:
            msg = "Adding %s to %s in %s." 
            logging.debug(msg, construct, self.construct, context)
