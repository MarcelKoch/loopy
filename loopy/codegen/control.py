"""Loop nest build top-level control/hoisting."""

from __future__ import division
from __future__ import absolute_import
import six

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

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


from loopy.codegen import gen_code_block
import islpy as isl
from loopy.schedule import (EnterLoop, LeaveLoop, RunInstruction, Barrier,
        gather_schedule_subloop, generate_sub_sched_items)


def get_admissible_conditional_inames_for(kernel, sched_index):
    """This function disallows conditionals on local-idx tagged
    inames if there is a barrier nested somewhere within.
    """

    from loopy.kernel.data import LocalIndexTag, HardwareParallelTag

    from loopy.schedule import find_active_inames_at, has_barrier_within
    result = find_active_inames_at(kernel, sched_index)

    has_barrier = has_barrier_within(kernel, sched_index)

    for iname, tag in six.iteritems(kernel.iname_to_tag):
        if isinstance(tag, HardwareParallelTag):
            if not has_barrier or not isinstance(tag, LocalIndexTag):
                result.add(iname)

    return result


def generate_code_for_sched_index(kernel, sched_index, codegen_state):
    sched_item = kernel.schedule[sched_index]

    if isinstance(sched_item, EnterLoop):
        tag = kernel.iname_to_tag.get(sched_item.iname)

        from loopy.codegen.loop import (
                generate_unroll_loop,
                generate_sequential_loop_dim_code)

        from loopy.kernel.data import (UnrolledIlpTag, UnrollTag, ForceSequentialTag,
                LoopedIlpTag)
        if isinstance(tag, (UnrollTag, UnrolledIlpTag)):
            func = generate_unroll_loop
        elif tag is None or isinstance(tag, (LoopedIlpTag, ForceSequentialTag)):
            func = generate_sequential_loop_dim_code
        else:
            raise RuntimeError("encountered (invalid) EnterLoop "
                    "for '%s', tagged '%s'" % (sched_item.iname, tag))

        return func(kernel, sched_index, codegen_state)

    elif isinstance(sched_item, Barrier):
        from loopy.codegen import GeneratedInstruction
        from cgen import Statement as S

        if sched_item.comment:
            comment = " /* %s */" % sched_item.comment
        else:
            comment = ""

        return GeneratedInstruction(
                ast=S("barrier(CLK_LOCAL_MEM_FENCE)%s" % comment),
                implemented_domain=None)

    elif isinstance(sched_item, RunInstruction):
        insn = kernel.id_to_insn[sched_item.insn_id]

        from loopy.codegen.instruction import generate_instruction_code
        return generate_instruction_code(kernel, insn, codegen_state)

    else:
        raise RuntimeError("unexpected schedule item type: %s"
                % type(sched_item))


def remove_inames_for_shared_hw_axes(kernel, cond_inames):
    """
    See if cond_inames contains references to two (or more) inames that
    boil down to the same tag. If so, exclude them. (We shouldn't be writing
    conditionals for such inames because we would be implicitly restricting
    the other inames as well.)
    """

    tag_key_uses = {}

    from loopy.kernel.data import HardwareParallelTag

    for iname in cond_inames:
        tag = kernel.iname_to_tag.get(iname)

        if isinstance(tag, HardwareParallelTag):
            tag_key_uses.setdefault(tag.key, []).append(iname)

    multi_use_keys = set(
            key for key, user_inames in six.iteritems(tag_key_uses)
            if len(user_inames) > 1)

    multi_use_inames = set()
    for iname in cond_inames:
        tag = kernel.iname_to_tag.get(iname)
        if isinstance(tag, HardwareParallelTag) and tag.key in multi_use_keys:
            multi_use_inames.add(iname)

    return frozenset(cond_inames - multi_use_inames)


def get_required_predicates(kernel, sched_index):
    result = None
    for _, sched_item in generate_sub_sched_items(kernel.schedule, sched_index):
        if isinstance(sched_item, Barrier):
            my_preds = frozenset()
        elif isinstance(sched_item, RunInstruction):
            my_preds = kernel.id_to_insn[sched_item.insn_id].predicates
        else:
            raise RuntimeError("unexpected schedule item type: %s"
                    % type(sched_item))

        if result is None:
            result = my_preds
        else:
            result = result & my_preds

    return result


def build_loop_nest(kernel, sched_index, codegen_state):
    # Most of the complexity of this function goes towards finding groups of
    # instructions that can be nested inside a shared conditional.

    # {{{ pass 1: pre-scan schedule for my schedule item's siblings' indices

    # i.e. go up to the next LeaveLoop, and skip over inner loops.

    my_sched_indices = []

    while sched_index < len(kernel.schedule):
        sched_item = kernel.schedule[sched_index]

        if isinstance(sched_item, LeaveLoop):
            break

        my_sched_indices.append(sched_index)

        if isinstance(sched_item, EnterLoop):
            _, sched_index = gather_schedule_subloop(
                    kernel.schedule, sched_index)
        elif isinstance(sched_item, Barrier):
            sched_index += 1

        elif isinstance(sched_item, RunInstruction):
            sched_index += 1
        else:
            raise RuntimeError("unexpected schedule item type: %s"
                    % type(sched_item))

    # }}}

    # {{{ pass 2: find admissible conditional inames for each sibling schedule item

    from pytools import Record

    class ScheduleIndexInfo(Record):
        """
        .. attribute:: schedule_index
        .. attribute:: admissible_cond_inames
        .. attribute:: required_predicates
        """

    sched_index_info_entries = [
            ScheduleIndexInfo(
                schedule_index=i,
                admissible_cond_inames=(
                    get_admissible_conditional_inames_for(kernel, i)),
                required_predicates=get_required_predicates(kernel, i)
                )
            for i in my_sched_indices]

    # }}}

    # {{{ pass 3: greedily group schedule items that share admissible inames

    from pytools import memoize_method

    class BoundsCheckCache:
        def __init__(self, kernel, impl_domain):
            self.kernel = kernel
            self.impl_domain = impl_domain

        @memoize_method
        def __call__(self, check_inames):
            if not check_inames:
                return []

            domain = isl.align_spaces(
                    self.kernel.get_inames_domain(check_inames),
                    self.impl_domain, obj_bigger_ok=True)
            from loopy.codegen.bounds import get_bounds_checks
            return get_bounds_checks(domain,
                    check_inames, self.impl_domain,

                    # Each instruction individually gets its bounds checks,
                    # so we can safely overapproximate here.
                    overapproximate=True)

    def build_insn_group(sched_index_info_entries, codegen_state,
            done_group_lengths=set()):
        """
        :arg done_group_lengths: A set of group lengths (integers) that grows from
            empty to include 1 and upwards with every recursive call.
            It serves to prevent infinite recursion by preventing recursive
            calls from doing anything about groups that are too small.
        """

        # The rough plan here is that build_insn_group starts out with the
        # entirety of the current schedule item's downward siblings (i.e. all
        # the ones up to the next LeaveLoop). It will then iterate upward to
        # find the largest usable conditional hoist group.
        #
        # It will then call itself recursively, telling its recursive instances
        # to ignore the hoist group it just found by adding that group length
        # to done_group_length. (It'll also chop the set of schedule indices
        # considered down so that a callee cannot find a *longer* hoist group.)
        #
        # Upon return the hoist is wrapped around the returned code and
        # build_insn_group calls itself for the remainder of schedule indices
        # that were not in the hoist group.

        if not sched_index_info_entries:
            return []

        si_entry = sched_index_info_entries[0]
        sched_index = si_entry.schedule_index
        current_iname_set = si_entry.admissible_cond_inames
        current_pred_set = (si_entry.required_predicates
                - codegen_state.implemented_predicates)

        # {{{ grow schedule item group

        # Keep growing schedule item group as long as group fulfills minimum
        # size requirement.

        bounds_check_cache = BoundsCheckCache(
                kernel, codegen_state.implemented_domain)

        found_hoists = []

        candidate_group_length = 1
        while candidate_group_length <= len(sched_index_info_entries):
            if candidate_group_length in done_group_lengths:
                candidate_group_length += 1
                continue

            current_iname_set = (
                    current_iname_set
                    & sched_index_info_entries[candidate_group_length-1]
                    .admissible_cond_inames)
            current_pred_set = (
                    current_pred_set
                    & sched_index_info_entries[candidate_group_length-1]
                    .required_predicates)

            # {{{ see which inames are actually used in group

            # And only generate conditionals for those.
            from loopy.schedule import find_used_inames_within
            used_inames = set()
            for sched_index_info_entry in \
                    sched_index_info_entries[0:candidate_group_length]:
                used_inames |= find_used_inames_within(kernel,
                        sched_index_info_entry.schedule_index)

            # }}}

            only_unshared_inames = remove_inames_for_shared_hw_axes(kernel,
                    current_iname_set & used_inames)

            bounds_checks = bounds_check_cache(only_unshared_inames)

            if (bounds_checks  # found a bounds check
                    or bounds_checks is None  # found impossible bounds check
                    or current_pred_set
                    or candidate_group_length == 1):
                # length-1 must always be an option to reach the recursion base
                # case below
                found_hoists.append((candidate_group_length,
                    bounds_checks, current_pred_set))

            candidate_group_length += 1

        # }}}

        # pick largest such group
        group_length, bounds_checks, pred_checks = max(found_hoists)

        check_set = None
        for cns in bounds_checks:
            cns_set = (isl.BasicSet.universe(cns.get_space())
                    .add_constraint(cns))

            if check_set is None:
                check_set = cns_set
            else:
                check_set, cns_set = isl.align_two(check_set, cns_set)
                check_set = check_set.intersect(cns_set)

        if check_set is None:
            new_codegen_state = codegen_state
            is_empty = False
        else:
            is_empty = check_set.is_empty()
            new_codegen_state = codegen_state.intersect(check_set)

        if pred_checks:
            new_codegen_state = new_codegen_state.copy(
                    implemented_predicates=new_codegen_state.implemented_predicates
                    | pred_checks)

        if is_empty:
            result = []
        else:
            if group_length == 1:
                # group only contains starting schedule item
                result = [generate_code_for_sched_index(
                    kernel, sched_index, new_codegen_state)]
                if result == [None]:
                    result = []
            else:
                # recurse with a bigger done_group_lengths
                result = build_insn_group(
                        sched_index_info_entries[0:group_length],
                        new_codegen_state,
                        done_group_lengths=done_group_lengths | set([group_length]))

            if bounds_checks or pred_checks:
                from loopy.codegen import wrap_in_if
                from loopy.codegen.bounds import constraint_to_code

                conditionals = [
                        constraint_to_code(codegen_state.c_code_mapper, cns)
                        for cns in bounds_checks] + list(pred_checks)

                result = [wrap_in_if(conditionals, gen_code_block(result))]

        return result + build_insn_group(
                sched_index_info_entries[group_length:], codegen_state)

    # }}}

    return gen_code_block(
            build_insn_group(sched_index_info_entries, codegen_state))


# vim: foldmethod=marker
