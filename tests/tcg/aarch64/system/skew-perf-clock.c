/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Instrument ONLY the marker store, never the compute/synchronization loops. */
#include <qemu-plugin.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <stdatomic.h>
QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
static uint64_t marker_pc, done_pc, start_ns;
static _Atomic unsigned state;
static void mark(unsigned cpu, qemu_plugin_meminfo_t info,
                 uint64_t vaddr, void *opaque)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint64_t now=(uint64_t)ts.tv_sec*1000000000+ts.tv_nsec;
    /* v7 has no memory-value API: distinct store PCs identify START/DONE. */
    if(vaddr!=0x09000000 || qemu_plugin_mem_size_shift(info)!=2 ||
       !qemu_plugin_mem_is_store(info)) abort();
    if(opaque==NULL && state==0 && cpu==0) { start_ns=now; state=1; }
    else if(opaque!=NULL && state==1) {
        state=2;
        fprintf(stderr,"HOST_BENCH_NS %llu\n",(unsigned long long)(now-start_ns));
        /* Printed after DONE; align optional trace to the measured interval. */
        fprintf(stderr,"HOST_BENCH_RANGE %llu %llu\n",
                (unsigned long long)start_ns,(unsigned long long)now);
    } else abort();
}
static void translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    for(size_t i=0;i<qemu_plugin_tb_n_insns(tb);i++) {
        struct qemu_plugin_insn *insn=qemu_plugin_tb_get_insn(tb,i);
        if(qemu_plugin_insn_vaddr(insn)==marker_pc)
            qemu_plugin_register_vcpu_mem_cb(insn,mark,QEMU_PLUGIN_CB_NO_REGS,
                                             QEMU_PLUGIN_MEM_W,NULL);
        else if(qemu_plugin_insn_vaddr(insn)==done_pc)
            qemu_plugin_register_vcpu_mem_cb(insn,mark,QEMU_PLUGIN_CB_NO_REGS,
                                             QEMU_PLUGIN_MEM_W,(void *)1);
    }
}
QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                         const qemu_info_t *info,int argc,char **argv)
{
    if(argc!=2) return -1;
    marker_pc=strtoull(argv[0],NULL,16);
    done_pc=strtoull(argv[1],NULL,16);
    if(!marker_pc || !done_pc || marker_pc==done_pc) return -1;
    qemu_plugin_register_vcpu_tb_trans_cb(id,translate);
    return 0;
}
