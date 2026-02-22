/* Linker script for Spike ISA simulator
 * Spike has built-in devices at low addresses (0x0-0x2000) that conflict
 * with Coral's ITCM at 0x0. For Spike, we place code at 0x80000000
 * (Spike's default memory region) and data at 0x80010000.
 *
 * The run.py evaluator uses this for Spike, and link.ld for Coral's mem images.
 */

MEMORY
{
    ITCM (rx)  : ORIGIN = 0x80000000, LENGTH = 64K
    DTCM (rw)  : ORIGIN = 0x80010000, LENGTH = 64K
}

ENTRY(_start)

SECTIONS
{
    /* Code goes to ITCM */
    .text : {
        *(.text.init)
        *(.text)
        *(.text.*)
    } > ITCM

    /* TOHOST section for Spike HTIF */
    .tohost : {
        *(.tohost)
    } > DTCM

    /* Read-only data in ITCM (after code) */
    .rodata : {
        *(.rodata)
        *(.rodata.*)
    } > ITCM

    /* Initialized data in DTCM */
    .data : {
        *(.data)
        *(.data.*)
        *(.sdata)
        *(.sdata.*)
    } > DTCM

    /* Uninitialized data in DTCM */
    .bss : {
        __bss_start = .;
        *(.bss)
        *(.bss.*)
        *(.sbss)
        *(.sbss.*)
        *(COMMON)
        __bss_end = .;
    } > DTCM

    /* Stack at end of DTCM */
    .stack (NOLOAD) : {
        . = ALIGN(16);
        _stack_end = .;
        . = . + 4K;
        _stack_start = .;
    } > DTCM
}
