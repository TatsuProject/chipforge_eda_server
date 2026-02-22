/* Linker script for Coral NPU — code at 0x0 for direct ITCM loading */
/* Memory Map:
 *   ITCM: 0x0000_0000 - 0x0000_FFFF (64 KB) - Instructions
 *   DTCM: 0x0001_0000 - 0x0001_FFFF (64 KB) - Data
 */

MEMORY
{
    ITCM (rx)  : ORIGIN = 0x00000000, LENGTH = 64K
    DTCM (rw)  : ORIGIN = 0x00010000, LENGTH = 64K
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

    /* TOHOST section for Spike HTIF - must be in DTCM */
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
