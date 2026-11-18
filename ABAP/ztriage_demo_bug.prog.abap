*&---------------------------------------------------------------------*
*& Report ZTRIAGE_DEMO_BUG
*&---------------------------------------------------------------------*
*& ABAP Triage MVP - demo program with INTENTIONAL errors.
*& Used to produce cancelled background jobs for the triage system.
*&
*& P_MODE = C  Unit price calculation. BUG: P_QTY = 0 -> division by
*&             zero short dump (category: Developer / program bug)
*& P_MODE = M  Material check. Material P_MATNR does not exist ->
*&             error message, job cancelled (category: Functional/data)
*& P_MODE = R  Remote call to RFC destination P_DEST that does not exist
*&             -> RFC short dump (category: Basis / connection)
*&
*& Suggested variants: ZBUG_DIV0 (C, qty 0), ZBUG_DATA (M, unknown
*& material), ZBUG_RFC (R), ZOK (C, qty 10).
*&---------------------------------------------------------------------*
REPORT ztriage_demo_bug.

PARAMETERS:
  p_mode  TYPE c LENGTH 1 DEFAULT 'C' OBLIGATORY,
  p_total TYPE i DEFAULT 1000,
  p_qty   TYPE i DEFAULT 10,
  p_matnr TYPE mara-matnr,
  p_dest  TYPE rfcdest DEFAULT 'ZTRIAGE_DUMMY'.

CLASS lcl_demo DEFINITION FINAL.
  PUBLIC SECTION.
    METHODS run.
  PRIVATE SECTION.
    METHODS calc_unit_price.
    METHODS check_material.
    METHODS call_remote.
ENDCLASS.

CLASS lcl_demo IMPLEMENTATION.
  METHOD run.
    WRITE: / 'ZTRIAGE_DEMO_BUG - mode:', p_mode.
    CASE p_mode.
      WHEN 'C'.
        calc_unit_price( ).
      WHEN 'M'.
        check_material( ).
      WHEN 'R'.
        call_remote( ).
      WHEN OTHERS.
        MESSAGE |Unknown mode { p_mode }. Use C, M or R.| TYPE 'E'.
    ENDCASE.
    WRITE: / 'Finished successfully.'.
  ENDMETHOD.

  METHOD calc_unit_price.
    DATA lv_unit_price TYPE i.
    WRITE: / 'Total amount:', p_total, / 'Quantity    :', p_qty.
    " BUG (intentional): no check for quantity = 0
    lv_unit_price = p_total / p_qty.
    WRITE: / 'Unit price  :', lv_unit_price.
  ENDMETHOD.

  METHOD check_material.
    SELECT SINGLE matnr, mtart FROM mara
      WHERE matnr = @p_matnr
      INTO @DATA(ls_mara).
    IF sy-subrc <> 0.
      " Data issue: material master missing -> job cancelled
      MESSAGE |Material { p_matnr ALPHA = OUT } does not exist in material master (MARA)| TYPE 'E'.
    ENDIF.
    WRITE: / 'Material', ls_mara-matnr, 'type', ls_mara-mtart, 'found.'.
  ENDMETHOD.

  METHOD call_remote.
    WRITE: / 'Calling RFC_PING on destination', p_dest.
    " Connection issue: destination missing/unreachable -> RFC dump
    CALL FUNCTION 'RFC_PING' DESTINATION p_dest.
    WRITE: / 'Remote system reachable.'.
  ENDMETHOD.
ENDCLASS.

START-OF-SELECTION.
  NEW lcl_demo( )->run( ).
