FUNCTION Z_TRIAGE_TR_RELEASE
  IMPORTING
    VALUE(IV_TRKORR) TYPE TRKORR
  EXPORTING
    VALUE(EV_RELEASED) TYPE FLAG
    VALUE(EV_STATUS) TYPE TRSTATUS
    VALUE(ET_MESSAGES) TYPE BAPIRET2_T.


*----------------------------------------------------------------------*
* ABAP Triage MVP - release a transport request (tasks first, then the
* request). RELEASE ONLY: the import into UAT follows the normal TMS
* process. Called by the local fix worker after human approval.
* EV_RELEASED = X when the request is released (status R or O).
*----------------------------------------------------------------------*
  DATA lv_msg TYPE bapi_msg.

  CLEAR: ev_released, ev_status, et_messages.

  SELECT SINGLE trkorr, trstatus, strkorr FROM e070
    WHERE trkorr = @iv_trkorr
    INTO @DATA(ls_req).
  IF sy-subrc <> 0.
    APPEND VALUE #( type = 'E' message = |Transport request { iv_trkorr } does not exist| ) TO et_messages.
    RETURN.
  ENDIF.
  ev_status = ls_req-trstatus.

  IF ls_req-strkorr IS NOT INITIAL.
    APPEND VALUE #( type = 'E' message = |{ iv_trkorr } is a task of { ls_req-strkorr }; pass the request number| ) TO et_messages.
    RETURN.
  ENDIF.

  IF ls_req-trstatus = 'R' OR ls_req-trstatus = 'O' OR ls_req-trstatus = 'N'.
    ev_released = abap_true.
    APPEND VALUE #( type = 'W' message = |Transport request { iv_trkorr } is already released (status { ls_req-trstatus })| ) TO et_messages.
    RETURN.
  ENDIF.

  " 1) release open tasks, 2) release the request
  SELECT trkorr FROM e070
    WHERE strkorr = @iv_trkorr
      AND trstatus IN ('D', 'L')
    INTO TABLE @DATA(lt_task).

  DATA(lt_release) = VALUE trkorrs( FOR t IN lt_task ( t-trkorr ) ).
  APPEND iv_trkorr TO lt_release.

  LOOP AT lt_release INTO DATA(lv_trkorr).
    CALL FUNCTION 'TR_RELEASE_REQUEST'
      EXPORTING
        iv_trkorr                  = lv_trkorr
        iv_dialog                  = abap_false
        iv_as_background_job       = abap_false
        iv_success_message         = abap_false
        iv_display_export_log      = abap_false
      EXCEPTIONS
        cts_initialization_failure = 1
        enqueue_failed             = 2
        no_authorization           = 3
        invalid_request            = 4
        request_already_released   = 5
        repeat_too_early           = 6
        error_in_export_methods    = 7
        object_check_error         = 8
        docu_missing               = 9
        db_access_error            = 10
        action_aborted_by_user     = 11
        export_failed              = 12
        OTHERS                     = 13.
    IF sy-subrc = 5.
      CONTINUE.
    ELSEIF sy-subrc <> 0.
      DATA(lv_rc) = sy-subrc.
      IF sy-msgid IS NOT INITIAL.
        MESSAGE ID sy-msgid TYPE 'E' NUMBER sy-msgno
          WITH sy-msgv1 sy-msgv2 sy-msgv3 sy-msgv4 INTO lv_msg.
      ENDIF.
      APPEND VALUE #( type = 'E' id = sy-msgid number = sy-msgno
                      message = |Release of { lv_trkorr } failed (rc { lv_rc }): { lv_msg }| ) TO et_messages.
      EXIT.
    ENDIF.
    APPEND VALUE #( type = 'S' message = |{ lv_trkorr } released| ) TO et_messages.
  ENDLOOP.

  SELECT SINGLE trstatus FROM e070 WHERE trkorr = @iv_trkorr INTO @ev_status.
  ev_released = xsdbool( ev_status = 'R' OR ev_status = 'O' OR ev_status = 'N' ).
ENDFUNCTION.
