/* Copyright 2015-2021 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * This modules controls HTML comments in order to make them editable. To do
 * so, it requires some definitions (see test_messages.edit.html file for the
 * complete structure reference):
 *  - A div container with the class .editable-message containing everything
 *      else related to the message
 *  - A data-baseurl="/path/to/msg" on the .editable-message container
 *  - A data-i-can-edit="True|False" on the .editable-message container
 *  - A .editable-message-body container with the original msg content
 *  - A .editable-message-edit-btn element inside the main container, that will
 *      switch the view to edit form when clicked.
 *  - A .editable-message-form, with a textarea and 2 buttons:
 *      .editable-message-update-btn and  .editable-message-cancel-btn.
 *  - A .editable-message-last-edit-date span, where we update the date of the
 *       last message editing.
 *
 * For the message revision history, you should define:
 *  - A .editable-message-last-edit-link, that will show the revisions pop-up
 *      once clicked.
 *  - A .message-revision-container, holding the header and the body of the
 *      revision list, and the template for each revision item.
 *  - A .message-revision-container-header.
 *  - A .message-revision-list, where the revisions will be placed.
 *  - A <script type="text/template">, with the definition of how each item
 *      should look like.
 *
 * Once those HTML elements are available in the page, this module should be
 * initialized with `lp.services.messages.edit.setup()`.
 *
 * @module Y.lp.services.messages.edit
 * @requires node, DOM, lp.client
 */
/* eslint-disable @microsoft/sdl/no-inner-html --
   All innerHTML writes use escaped content (htmlify_msg), hardcoded
   strings, or empty string clears. No unsanitized user input. */
YUI.add('lp.services.messages.edit', function(Y) {
    var module = Y.namespace('lp.services.messages.edit');

    module.msg_edit_error_notification = (
        "There was an error updating the comment. " +
        "Please try again in a few minutes."
    );

    module.confirm_delete_revision_msg = (
        "Are you sure you want to delete this "+
        "revision content?");

    module.deleted_content_msg = (
        "<span class='deleted-content'>" +
        "Content deleted by the user." +
        "</span>"
    );

    module.confirm_delete_message = (
        "Are you sure you want to delete this "+
        "comment and all its revisions?");

    // Making it easier to mock on tests.
    module.confirm = function(msg) {
        return confirm(msg);
    };

    module.htmlify_msg = function(text) {
        text = text.replace(/&/g, "&amp;");
        text = text.replace(/</g, "&lt;");
        text = text.replace(/>/g, "&gt;");
        text = text.replace(/\n/g, "<br/>");
        return "<p>" + text    + "</p>";
    };

    module.showEditMessageField = function(msg_body, msg_form) {
        msg_body.setStyle('display', 'none');
        msg_form.setStyle('display', 'block');
    };

    module.hideEditMessageField = function(msg_body, msg_form) {
        msg_body.setStyle('display', 'block');
        msg_form.setStyle('display', 'none');
    };

    module.saveMessageContent = function(
            msg_path, new_content, on_success, on_failure) {
        var msg_url = "/api/devel" + msg_path;
        var config = {
            on: {
                 success: on_success,
                 failure: on_failure
             },
            parameters: {"new_content": new_content}
        };
        this.lp_client.named_post(msg_url, 'editContent', config);
    };

    module.deleteMessageContent = function(
            msg_path, on_success, on_failure) {
        var msg_url = "/api/devel" + msg_path;
        if (module.confirm(module.confirm_delete_message)) {
            var config = {
                on: {
                     success: on_success,
                     failure: on_failure
                 }
            };
            this.lp_client.named_post(msg_url, 'deleteContent', config);
        }
    };

    module.showNotification = function(container, msg, can_dismiss) {
        can_dismiss = can_dismiss || false;
        // Clean up previous notification.
        module.hideNotification(container);
        container.setStyle('position', 'relative');
        var node = Y.Node.create(
            "<div class='editable-message-notification'>" +
            "  <p class='block-sprite large-warning'>" +
            msg +
            "  </p>" +
            "</div>");
         container.append(node);
         if (can_dismiss) {
             var dismiss = Y.Node.create(
                "<div class='editable-message-notification-dismiss'>" +
                "  <input type='button' value='Ok' />" +
                "</div>");
             dismiss.on('click', function() {
                module.hideNotification(container);
             });
             node.append(dismiss);
         }
    };

    module.hideNotification = function(container) {
        var notification = container.one(".editable-message-notification");
        if(notification) {
            notification.remove();
        }
    };

    module.showLoading = function(container) {
        module.showNotification(
            container,
            '<img class="spinner" src="/@@/spinner" alt="Loading..." />');
    };

    module.hideLoading = function(container) {
        module.hideNotification(container);
    };

    // What to do when a user clicks a message's "edit" button.
    module.onEditClick = function(elements) {
        // When clicking edit icon, show the edit form and focus on the
        // text area.
        module.showEditMessageField(elements.msg_body, elements.msg_form);
        elements.msg_form.one('textarea').getDOMNode().focus();
    };

    // What to do when a user clicks "cancel edit" button.
    module.onEditCancelClick = function(elements) {
        module.hideEditMessageField(elements.msg_body, elements.msg_form);
    };

    // What to do when a user clicks the update button after editing a msg.
    module.onUpdateClick = function(elements, baseurl) {
        // When clicking on "update" button, disable UI elements and send a
        // request to update the message at the backend.
        module.showLoading(elements.container);
        var textarea = elements.textarea.getDOMNode();
        var new_content = textarea.value;
        textarea.disabled = true;
        elements.update_btn.getDOMNode().disabled = true;

        module.saveMessageContent(
            baseurl, new_content,
            function(err) {
                module.onMessageSaved(elements, new_content, baseurl);
            },
            function(err) { module.onMessageSaveError(elements, err); }
        );
    };

    // What to do when a message is saved in the backend.
    module.onMessageSaved = function(elements, new_content, baseurl) {
        // When finished updating at the backend, re-enable UI
        // elements and display the new message.
        var html_msg = module.htmlify_msg(new_content);
        elements.msg_body_text.getDOMNode().innerHTML = html_msg;
        module.hideEditMessageField(
            elements.msg_body, elements.msg_form);
        elements.textarea.getDOMNode().disabled = false;
        elements.update_btn.getDOMNode().disabled = false;
        module.hideLoading(elements.container);
        elements.last_edit.getDOMNode().innerHTML = (
            ' <a href="#" class="editable-message-last-edit-link">' +
            '(last edit a moment ago):' +
            '</a>');

        // Wire click handler to the newly created "last edit" button.
        var last_edit_btn = elements.container.one(
            '.editable-message-last-edit-link');
        last_edit_btn.on('click', function(e) {
            e.preventDefault();
            module.onLastEditClick(elements, baseurl);
        });
    };

    // What to do when a message is deleted in the backend.
    module.onMessageDeleted = function(elements, baseurl) {
        elements.msg_body_text.getDOMNode().innerHTML = '';
        // if after a delete the user wants to edit the empty
        // message placeholder, they won't be able to
        // unless we enable the text area and update button
        elements.textarea.getDOMNode().disabled = false;
        elements.update_btn.getDOMNode().disabled = false;
        module.hideLoading(elements.container);
        elements.last_edit.getDOMNode().innerHTML = (
            ' <a href="#" class="editable-message-last-edit-link">' +
            '(message and all revisions deleted a moment ago):' +
            '</a>');

        // Wire click handler to the newly created
        // "message deleted / last edit" button.
        var last_edit_btn = elements.container.one(
            '.editable-message-last-edit-link');
        last_edit_btn.on('click', function(e) {
            e.preventDefault();
            module.onLastEditClick(elements, baseurl);
        });
    };

    // What to do when a message fails to delete on the backend.
    module.onMessageDeleteError = function(elements, err) {
        // When something goes wrong at the backend, re-enable
        // UI elements and display an error.
        module.showNotification(
            elements.container,
            module.msg_edit_error_notification,  true);
        elements.textarea.getDOMNode().disabled = false;
        elements.update_btn.getDOMNode().disabled = false;
    };

    // What to do when a message fails to update on the backend.
    module.onMessageSaveError = function(elements, err) {
        // When something goes wrong at the backend, re-enable
        // UI elements and display an error.
        module.showNotification(
            elements.container,
            module.msg_edit_error_notification,  true);
        elements.textarea.getDOMNode().disabled = false;
        elements.update_btn.getDOMNode().disabled = false;
    };

    // What to do when a user clicks the delete button on a msg.
    module.onDeleteClick = function(elements, baseurl) {
        module.showLoading(elements.container);
        var textarea = elements.textarea.getDOMNode();
        textarea.disabled = true;
        elements.delete_btn.getDOMNode().disabled = true;

        module.deleteMessageContent(
            baseurl,
            function(err) {
                module.onMessageDeleted(elements, baseurl);
            },
            function(err) { module.onMessageDeleteError(elements, err); }
        );
    };
    module.fillMessageRevisions = function(elements, revisions) {
        // Position the message revision list element.
        revisions = revisions.reverse();
        var i_can_edit = elements.container.getData('i-can-edit') === "True";
        var revisions_container = elements.container.one(
            ".message-revision-container");
        var last_edit_el = elements.last_edit.getDOMNode();
        var target_position = last_edit_el.getBoundingClientRect();
        var nodes_holder = revisions_container.one(".message-revision-list");
        var template = revisions_container.one(
            "script[type='text/template']").getDOMNode().innerHTML;

        revisions_container.setStyle('left', target_position.left);
        revisions_container.setStyle('display', 'block');
        revisions_container.one(".message-revision-close").on(
            "click", function() {
                nodes_holder.getDOMNode().innerHTML = '';
                revisions_container.setStyle('display', 'none');
            });

        nodes_holder.getDOMNode().innerHTML = "";
        revisions.forEach(function(rev) {
            var attrs = rev.getAttrs();
            if (!attrs.date_deleted) {
                attrs.content = module.htmlify_msg(attrs.content);
            }
            else {
                attrs.content = module.deleted_content_msg;
            }
            var node = Y.DOM.create(Y.Lang.sub(template, attrs));
            node.dataset['revision_url'] = attrs.self_link;
            nodes_holder.appendChild(node);

            if(attrs.date_deleted || !i_can_edit) {
                // If it was already deleted or I don't have permission to
                // delete it, remove the "delete button".
                module.removeDeleteRevisionButton(Y.Node(node));
            }
        });

        nodes_holder.all(".message-revision-item").each(function(rev_item) {
            rev_item.one(".message-revision-title").on('click', function() {
                nodes_holder.all('.message-revision-body').setStyle(
                    'display', 'none');
                var body = rev_item.one('.message-revision-body');
                var current_display = body.getStyle('display');
                body.setStyle(
                    'display', current_display === 'block'? 'none' : 'block');
                nodes_holder.all(".message-revision-item").removeClass(
                    'active');
                rev_item.addClass('active');
            });

            var delete_btn = rev_item.one(".message-revision-del-btn");
            if (delete_btn) {
                delete_btn.on('click', function() {
                    module.deleteMessageRevisionContent(rev_item);
                });
            }
        });
    };

    module.removeDeleteRevisionButton = function(rev_item_node) {
        var delete_btn = rev_item_node.one('.message-revision-del-btn');
        if (delete_btn) {
            var node_to_remove = delete_btn.getDOMNode();
            node_to_remove.parentNode.removeChild(node_to_remove);
        }
    };

    module.deleteMessageRevisionContent = function(rev_item) {
        var revision_url = rev_item.getData('revision_url');
        if (module.confirm(module.confirm_delete_revision_msg)) {
            var config = {
                on: {
                     success: function() {
                        var body_dom = rev_item.one(
                            '.message-revision-body').getDOMNode();
                        body_dom.innerHTML = module.deleted_content_msg;
                        module.removeDeleteRevisionButton(rev_item);
                     },
                     failure: function(err) {
                        alert("There was an error. Please try again.");
                     }
                 }
            };
            this.lp_client.named_post(revision_url, 'deleteContent', config);
        }
    };

    module.onLastEditClick = function(elements, baseurl) {
        // Hide all open revision containers.
        Y.all('.message-revision-container').each(function(container) {
            container.setStyle('display', 'none');
        });

        var url = "/api/devel" + baseurl + "/revisions";
        var config = {
            on: {
                 success: function(response) {
                    module.fillMessageRevisions(elements, response.entries);
                 },
                 failure: function(err) {
                    alert("Error fetching revisions.");
                 }
             },
             // XXX pappacena 2021-05-19: Pagination will be needed here.
             size: 100
        };
        this.lp_client.get(url, config);
    };

    module.wireEventHandlers = function(container) {
        var node = container.getDOMNode();
        var baseurl = node.dataset.baseurl;
        var elements = {
            "container": container,
            "msg_body": container.one('.editable-message-body'),
            "msg_body_text": container.one('.editable-message-text'),
            "msg_form": container.one('.editable-message-form'),
            "edit_btn": container.one('.editable-message-edit-btn'),
            "update_btn": container.one('.editable-message-update-btn'),
            "cancel_btn": container.one('.editable-message-cancel-btn'),
            "last_edit": container.one('.editable-message-last-edit-date'),
            "last_edit_btn": container.one('.editable-message-last-edit-link'),
            "delete_btn": container.one('.editable-message-delete-btn')
        };
        // If the msg body or the msg form are not defined, don't try to do
        // anything else.
        if (!elements.msg_form || !elements.msg_body) {
            return;
        }
        elements.textarea = elements.msg_form.one('textarea');

        module.hideEditMessageField(elements.msg_body, elements.msg_form);

        if (elements.last_edit_btn) {
            elements.last_edit_btn.on('click', function(e) {
                e.preventDefault();
                module.onLastEditClick(elements, baseurl);
            });
        }

        // If the edit button is not present, do not try to bind the
        // handlers.
        if (!elements.edit_btn || !baseurl) {
            return;
        }

        elements.edit_btn.on('click', function(e) {
            module.onEditClick(elements);
        });

        elements.update_btn.on('click', function(e) {
            module.onUpdateClick(elements, baseurl);
        });

        elements.cancel_btn.on('click', function(e) {
            module.onEditCancelClick(elements);
        });

        elements.delete_btn.on('click', function(e) {
            module.onDeleteClick(elements, baseurl);
        });
    };

    module.setup = function() {
        this.lp_client = new Y.lp.client.Launchpad();
        Y.all('.editable-message').each(module.wireEventHandlers);
    };
}, '0.1', {'requires': ['lp.client', 'node', 'DOM']});
