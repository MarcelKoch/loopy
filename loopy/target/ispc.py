"""Target for Intel ISPC."""

from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2015 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np  # noqa
from loopy.target.c import CTarget
from loopy.target.c.codegen.expression import LoopyCCodeMapper
from loopy.diagnostic import LoopyError
from pymbolic.mapper.stringifier import (PREC_SUM, PREC_CALL)

from pytools import memoize_method


# {{{ expression mapper

class LoopyISPCCodeMapper(LoopyCCodeMapper):
    def _get_index_ctype(self):
        if self.kernel.index_dtype.numpy_dtype == np.int32:
            return "int32"
        elif self.kernel.index_dtype.numpy_dtype == np.int64:
            return "int64"
        else:
            raise ValueError("unexpected index_type")

    def map_group_hw_index(self, expr, enclosing_prec, type_context):
        return "((uniform %s) taskIndex%d)" % (self._get_index_ctype(), expr.axis)

    def map_local_hw_index(self, expr, enclosing_prec, type_context):
        if expr.axis == 0:
            return "(varying %s) programIndex" % self._get_index_ctype()
        else:
            raise LoopyError("ISPC only supports one local axis")

    def map_constant(self, expr, enclosing_prec, type_context):
        if isinstance(expr, (complex, np.complexfloating)):
            raise NotImplementedError("complex numbers in ispc")
        else:
            if type_context == "f":
                return repr(float(expr))
            elif type_context == "d":
                # Keepin' the good ideas flowin' since '66.
                return repr(float(expr))+"d"
            elif type_context == "i":
                return str(int(expr))
            else:
                from loopy.tools import is_integer
                if is_integer(expr):
                    return str(expr)

                raise RuntimeError("don't know how to generated code "
                        "for constant '%s'" % expr)

    def map_variable(self, expr, enclosing_prec, type_context):
        if expr.name in self.kernel.temporary_variables:
            gsize, lsize = self.kernel.get_grid_sizes_as_exprs()
            if lsize:
                return "%s[programIndex]" % expr.name
            else:
                return expr.name
        else:
            return super(LoopyISPCCodeMapper, self).map_variable(
                    expr, enclosing_prec, type_context)

    def map_subscript(self, expr, enclosing_prec, type_context):
        from loopy.kernel.data import TemporaryVariable

        ary = self.find_array(expr)

        if isinstance(ary, TemporaryVariable):
            gsize, lsize = self.kernel.get_grid_sizes_as_exprs()
            if lsize:
                lsize, = lsize
                from loopy.kernel.array import get_access_info
                from pymbolic import evaluate

                access_info = get_access_info(self.kernel.target, ary, expr.index,
                    lambda expr: evaluate(expr, self.codegen_state.var_subst_map),
                    self.codegen_state.vectorization_info)

                subscript, = access_info.subscripts
                result = self.parenthesize_if_needed(
                        "%s[programIndex + %s]" % (
                            access_info.array_name,
                            self.rec(lsize*subscript, PREC_SUM, 'i')),
                        enclosing_prec, PREC_CALL)

                if access_info.vector_index is not None:
                    return self.kernel.target.add_vector_access(
                        result, access_info.vector_index)
                else:
                    return result

        return super(LoopyISPCCodeMapper, self).map_subscript(
                expr, enclosing_prec, type_context)

# }}}


# {{{ type registry

def fill_registry_with_ispc_types(reg, respect_windows, include_bool=True):
    reg.get_or_register_dtype("bool", np.bool)

    reg.get_or_register_dtype(["int8", "signed char", "char"], np.int8)
    reg.get_or_register_dtype(["uint8", "unsigned char"], np.uint8)
    reg.get_or_register_dtype(["int16", "short", "signed short",
        "signed short int", "short signed int"], np.int16)
    reg.get_or_register_dtype(["uint16", "unsigned short",
        "unsigned short int", "short unsigned int"], np.uint16)
    reg.get_or_register_dtype(["int32", "int", "signed int"], np.int32)
    reg.get_or_register_dtype(["uint32", "unsigned", "unsigned int"], np.uint32)

    reg.get_or_register_dtype(["int64"], np.int64)
    reg.get_or_register_dtype(["uint64"], np.uint64)

    reg.get_or_register_dtype("float", np.float32)
    reg.get_or_register_dtype("double", np.float64)

# }}}


class ISPCTarget(CTarget):
    """A code generation target for Intel's `ISPC <https://ispc.github.io/>`_
    SPMD programming language, to target Intel's Knight's hardware and modern
    Intel CPUs with wide vector units.
    """

    def __init__(self, occa_mode=False):
        """
        :arg occa_mode: Whether to modify the generated call signature to
            be compatible with OCCA
        """
        self.occa_mode = occa_mode

        super(ISPCTarget, self).__init__()

    # {{{ types

    @memoize_method
    def get_dtype_registry(self):
        from loopy.target.c.compyte.dtypes import DTypeRegistry
        result = DTypeRegistry()
        fill_registry_with_ispc_types(result, respect_windows=False,
                include_bool=True)
        return result

    # }}}

    # {{{ top-level codegen

    def generate_code(self, kernel, codegen_state, impl_arg_info):
        from cgen import (FunctionBody, FunctionDeclaration, Value, Module,
                Block, Line, Statement as S)
        from cgen.ispc import ISPCExport, ISPCTask

        knl_body, implemented_domains = kernel.target.generate_body(
                kernel, codegen_state)

        inner_name = "lp_ispc_inner_"+kernel.name
        arg_decls = [iai.cgen_declarator for iai in impl_arg_info]
        arg_names = [iai.name for iai in impl_arg_info]

        # {{{ occa compatibility hackery

        if self.occa_mode:
            from cgen import ArrayOf, Const
            from cgen.ispc import ISPCUniform

            arg_decls = [
                    Const(ISPCUniform(ArrayOf(Value("int", "loopy_dims")))),
                    Const(ISPCUniform(Value("int", "o1"))),
                    Const(ISPCUniform(Value("int", "o2"))),
                    Const(ISPCUniform(Value("int", "o3"))),
                    ] + arg_decls
            arg_names = ["loopy_dims", "o1", "o2", "o3"] + arg_names

        # }}}

        knl_fbody = FunctionBody(
                ISPCTask(
                    FunctionDeclaration(
                        Value("void", inner_name),
                        arg_decls)),
                knl_body)

        # {{{ generate wrapper

        wrapper_body = Block()

        gsize, lsize = kernel.get_grid_sizes_as_exprs()
        if len(lsize) > 1:
            for i, ls_i in enumerate(lsize[1:]):
                if ls_i != 1:
                    raise LoopyError("local axis %d (0-based) "
                            "has length > 1, which is unsupported "
                            "by ISPC" % ls_i)

        from pymbolic.mapper.stringifier import PREC_COMPARISON, PREC_NONE
        ccm = self.get_expression_to_code_mapper(codegen_state)

        if lsize:
            wrapper_body.append(
                    S("assert(programCount == %s)"
                        % ccm(lsize[0], PREC_COMPARISON)))

        if gsize:
            launch_spec = "[%s]" % ", ".join(
                                ccm(gs_i, PREC_NONE)
                                for gs_i in gsize)
        else:
            launch_spec = ""

        wrapper_body.append(
                S("launch%s %s(%s)"
                    % (
                        launch_spec,
                        inner_name,
                        ", ".join(arg_names)
                        ))
                )

        wrapper_fbody = FunctionBody(
                ISPCExport(
                    FunctionDeclaration(
                        Value("void", kernel.name),
                        arg_decls)),
                wrapper_body)

        # }}}

        mod = Module([
            knl_fbody,
            Line(),
            wrapper_fbody,
            ])

        return str(mod), implemented_domains

    # }}}

    # {{{ code generation guts

    def get_expression_to_code_mapper(self, codegen_state):
        return LoopyISPCCodeMapper(codegen_state)

    def add_vector_access(self, access_str, index):
        return "(%s)[%d]" % (access_str, index)

    def emit_barrier(self, kind, comment):
        from loopy.codegen import GeneratedInstruction
        from cgen import Comment, Statement

        assert comment

        if kind == "local":
            return GeneratedInstruction(
                    ast=Comment("local barrier: %s" % comment),
                    implemented_domain=None)

        elif kind == "global":
            return GeneratedInstruction(
                    ast=Statement("sync; /* %s */" % comment),
                    implemented_domain=None)

        else:
            raise LoopyError("unknown barrier kind")

    def get_temporary_decl(self, knl, temp_var, decl_info):
        from loopy.codegen import POD  # uses the correct complex type
        temp_var_decl = POD(self, decl_info.dtype, decl_info.name)

        shape = decl_info.shape

        from loopy.kernel.data import temp_var_scope
        if temp_var.scope == temp_var_scope.PRIVATE:
            gsize, lsize = knl.get_grid_sizes_as_exprs()
            shape = lsize + shape

        if shape:
            from cgen import ArrayOf
            temp_var_decl = ArrayOf(temp_var_decl,
                    " * ".join(str(s) for s in shape))

        return temp_var_decl

    def wrap_temporary_decl(self, decl, scope):
        from cgen.ispc import ISPCUniform
        return ISPCUniform(decl)

    def get_global_arg_decl(self, name, shape, dtype, is_written):
        from loopy.codegen import POD  # uses the correct complex type
        from cgen import Const
        from cgen.ispc import ISPCUniformPointer, ISPCUniform

        arg_decl = ISPCUniformPointer(POD(self, dtype, name))

        if not is_written:
            arg_decl = Const(arg_decl)

        arg_decl = ISPCUniform(arg_decl)

        return arg_decl

    def get_value_arg_decl(self, name, shape, dtype, is_written):
        result = super(ISPCTarget, self).get_value_arg_decl(
                name, shape, dtype, is_written)

        from cgen import Reference, Const
        was_const = isinstance(result, Const)

        if was_const:
            result = result.subdecl

        if self.occa_mode:
            result = Reference(result)

        if was_const:
            result = Const(result)

        from cgen.ispc import ISPCUniform
        return ISPCUniform(result)

    def emit_sequential_loop(self, codegen_state, iname, iname_dtype,
            static_lbound, static_ubound, inner):
        ecm = codegen_state.expression_to_code_mapper

        from loopy.symbolic import aff_to_expr

        from loopy.codegen import wrap_in
        from pymbolic.mapper.stringifier import PREC_NONE
        from cgen import For

        return wrap_in(For,
                "uniform %s %s = %s"
                % (self.dtype_to_typename(iname_dtype),
                    iname, ecm(aff_to_expr(static_lbound), PREC_NONE, "i")),
                "%s <= %s" % (
                    iname, ecm(aff_to_expr(static_ubound), PREC_NONE, "i")),
                "++%s" % iname,
                inner)
    # }}}

# TODO: Generate launch code
# TODO: Vector types (element access: done)

# vim: foldmethod=marker
