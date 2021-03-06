#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2009-2011 Umeå University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#            http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains classes and functions that a SAML2.0 Service Provider (SP) may use
to conclude its tasks.
"""
from saml2.samlp import logout_response_from_string
import saml2

try:
    from urlparse import parse_qs
except ImportError:
    # Compatibility with Python <= 2.5
    from cgi import parse_qs

from saml2.time_util import not_on_or_after

from saml2 import saml
from saml2 import class_name
from saml2.saml import AssertionIDRef
from saml2.saml import NAMEID_FORMAT_PERSISTENT
from saml2.sigver import pre_signature_part
from saml2.sigver import signed_instance_factory
from saml2.client_base import Base
from saml2.client_base import LogoutError
from saml2.client_base import NoServiceDefined
from saml2.mdstore import destinations

from saml2 import BINDING_HTTP_REDIRECT
from saml2 import BINDING_HTTP_POST
from saml2 import BINDING_SOAP

import logging
logger = logging.getLogger(__name__)

class Saml2Client(Base):
    """ The basic pySAML2 service provider class """

    def do_authenticate(self, entityid=None, relay_state="",
                     binding=saml2.BINDING_HTTP_REDIRECT, vorg="",
                     nameid_format=NAMEID_FORMAT_PERSISTENT,
                     scoping=None, consent=None, extensions=None, sign=None):
        """ Makes an authentication request.

        :param entityid: The entity ID of the IdP to send the request to
        :param relay_state: To where the user should be returned after
            successfull log in.
        :param binding: Which binding to use for sending the request
        :param vorg: The entity_id of the virtual organization I'm a member of
        :param scoping: For which IdPs this query are aimed.
        :param consent: Whether the principal have given her consent
        :param extensions: Possible extensions
        :param sign: Whether the request should be signed or not.
        :return: AuthnRequest response
        """

        location = self._sso_location(entityid, binding)

        req = self.create_authn_request(location, vorg, scoping, binding,
                                        nameid_format, consent, extensions,
                                        sign)
        _req_str = "%s" % req

        logger.info("AuthNReq: %s" % _req_str)

        if binding == saml2.BINDING_HTTP_POST:
            logger.info("HTTP POST")
            (header, body) = self.use_http_form_post(_req_str, location,
                                                 relay_state)
        elif binding == saml2.BINDING_HTTP_REDIRECT:
            logger.info("HTTP REDIRECT")
            (header, body) = self.use_http_get(_req_str, location,
                                                     relay_state)
        else:
            raise Exception("Unknown binding type: %s" % binding)

        return req.id, header, body

    def global_logout(self, subject_id, reason="", expire=None, sign=None):
        """ More or less a layer of indirection :-/
        Bootstrapping the whole thing by finding all the IdPs that should
        be notified.
        
        :param subject_id: The identifier of the subject that wants to be
            logged out.
        :param reason: Why the subject wants to log out
        :param expire: The latest the log out should happen.
            If this time has passed don't bother.
        :param sign: Whether the request should be signed or not.
            This also depends on what binding is used.
        :return: Depends on which binding is used:
            If the HTTP redirect binding then a HTTP redirect,
            if SOAP binding has been used the just the result of that
            conversation. 
        """

        logger.info("logout request for: %s" % subject_id)

        # find out which IdPs/AAs I should notify
        entity_ids = self.users.issuers_of_info(subject_id)

        return self.do_logout(subject_id, entity_ids, reason, expire, sign)
        
    def do_logout(self, subject_id, entity_ids, reason, expire, sign=None):
        """

        :param subject_id: Identifier of the Subject
        :param entity_ids: List of entity ids for the IdPs that have provided
            information concerning the subject
        :param reason: The reason for doing the logout
        :param expire: Try to logout before this time.
        :param sign: Whether to sign the request or not
        :return:
        """
        # check time
        if not not_on_or_after(expire): # I've run out of time
            # Do the local logout anyway
            self.local_logout(subject_id)
            return 0, "504 Gateway Timeout", [], []
            
        # for all where I can use the SOAP binding, do those first
        not_done = entity_ids[:]
        responses = {}

        for entity_id in entity_ids:
            response = False

            for binding in [#BINDING_SOAP,
                            BINDING_HTTP_POST,
                            BINDING_HTTP_REDIRECT]:
                srvs = self.metadata.single_logout_service(entity_id, "idpsso",
                                                           binding=binding)
                if not srvs:
                    continue

                destination = destinations(srvs)[0]

                logger.info("destination to provider: %s" % destination)
                request = self.create_logout_request(destination, entity_id,
                                                     subject_id, reason=reason,
                                                     expire=expire)
                
                to_sign = []
                if binding.startswith("http://"):
                    sign = True

                if sign is None:
                    sign = self.logout_requests_signed_default

                if sign:
                    request.signature = pre_signature_part(request.id,
                                                    self.sec.my_cert, 1)
                    to_sign = [(class_name(request), request.id)]

                logger.info("REQUEST: %s" % request)

                srequest = signed_instance_factory(request, self.sec, to_sign)
        
                if binding == BINDING_SOAP:
                    response = self.send_using_soap(srequest, destination)
                    if response:
                        logger.info("Verifying response")
                        response = self.logout_request_response(response)

                    if response:
                        not_done.remove(entity_id)
                        logger.info("OK response from %s" % destination)
                        responses[entity_id] = logout_response_from_string(response)
                    else:
                        logger.info("NOT OK response from %s" % destination)

                else:
                    session_id = request.id
                    rstate = self._relay_state(session_id)

                    self.state[session_id] = {"entity_id": entity_id,
                                              "operation": "SLO",
                                              "entity_ids": entity_ids,
                                              "subject_id": subject_id,
                                              "reason": reason,
                                              "not_on_of_after": expire,
                                              "sign": sign}
                    

                    if binding == BINDING_HTTP_POST:
                        response = self.use_http_form_post(srequest,
                                                           destination,
                                                           rstate)
                    else:
                        response = self.use_http_get(srequest, destination,
                                                     rstate)

                    responses[entity_id] = response
                    not_done.remove(entity_id)

                # only try one binding
                break

        if not_done:
            # upstream should try later
            raise LogoutError("%s" % (entity_ids,))
        
        return responses

    def local_logout(self, subject_id):
        """ Remove the user from the cache, equals local logout 
        
        :param subject_id: The identifier of the subject
        """
        self.users.remove_person(subject_id)
        return True

    def handle_logout_response(self, response):
        """ handles a Logout response 
        
        :param response: A response.Response instance
        :return: 4-tuple of (session_id of the last sent logout request,
            response message, response headers and message)
        """

        logger.info("state: %s" % (self.state,))
        status = self.state[response.in_response_to]
        logger.info("status: %s" % (status,))
        issuer = response.issuer()
        logger.info("issuer: %s" % issuer)
        del self.state[response.in_response_to]
        if status["entity_ids"] == [issuer]: # done
            self.local_logout(status["subject_id"])
            return 0, "200 Ok", [("Content-type","text/html")], []
        else:
            status["entity_ids"].remove(issuer)
            return self.do_logout(status["subject_id"], status["entity_ids"],
                                  status["reason"], status["not_on_or_after"],
                                  status["sign"])

    # MUST use SOAP for
    # AssertionIDRequest, SubjectQuery,
    # AuthnQuery, AttributeQuery, or AuthzDecisionQuery

    def use_soap(self, destination, query_type, **kwargs):
        _create_func = getattr(self, "create_%s" % query_type)
        _response_func = getattr(self, "%s_response" % query_type)
        try:
            response_args = kwargs["response_args"]
            del kwargs["response_args"]
        except KeyError:
            response_args = None

        query = _create_func(destination, **kwargs)

        response = self.send_using_soap(query, destination)

        if response:
            logger.info("Verifying response")
            if response_args:
                response = _response_func(response, **response_args)
            else:
                response = _response_func(response)

        if response:
            #not_done.remove(entity_id)
            logger.info("OK response from %s" % destination)
            return response
        else:
            logger.info("NOT OK response from %s" % destination)

        return None

    #noinspection PyUnusedLocal
    def do_authz_decision_query(self, entity_id, action,
                                subject_id, nameid_format,
                                evidence=None, resource=None,
                                sp_name_qualifier=None,
                                name_qualifier=None,
                                consent=None, extensions=None, sign=False):

        subject = saml.Subject(
            name_id = saml.NameID(text=subject_id,
                                  format=nameid_format,
                                  sp_name_qualifier=sp_name_qualifier,
                                  name_qualifier=name_qualifier))

        srvs = self.metadata.authz_service(entity_id, BINDING_SOAP)
        for dest in destinations(srvs):
            resp = self.use_soap(dest, "authz_decision_query",
                                 action=action, evidence=evidence,
                                 resource=resource, subject=subject)
            if resp:
                return resp

        return None

    def do_assertion_id_request(self, assertion_ids, entity_id,
                                consent=None, extensions=None, sign=False):

        srvs = self.metadata.assertion_id_request_service(entity_id,
                                                          BINDING_SOAP)
        if not srvs:
            raise NoServiceDefined("%s: %s" % (entity_id,
                                               "assertion_id_request_service"))

        if isinstance(assertion_ids, basestring):
            assertion_ids = [assertion_ids]

        _id_refs = [AssertionIDRef(_id) for _id in assertion_ids]

        for destination in destinations(srvs):
            res = self.use_soap(destination, "assertion_id_request",
                                assertion_id_refs=_id_refs, consent=consent,
                                extensions=extensions, sign=sign)
            if res:
                return res

        return None

    def do_authn_query(self, entity_id,
                       consent=None, extensions=None, sign=False):

        srvs = self.metadata.authn_request_service(entity_id, BINDING_SOAP)

        for destination in destinations(srvs):
            resp = self.use_soap(destination, "authn_query",
                                 consent=consent, extensions=extensions,
                                 sign=sign)
            if resp:
                return resp

        return None

    def do_attribute_query(self, entityid, subject_id,
                           attribute=None, sp_name_qualifier=None,
                           name_qualifier=None, nameid_format=None,
                           real_id=None, consent=None, extensions=None,
                           sign=False, binding=BINDING_SOAP):
        """ Does a attribute request to an attribute authority, this is
        by default done over SOAP.

        :param entityid: To whom the query should be sent
        :param subject_id: The identifier of the subject
        :param attribute: A dictionary of attributes and values that is asked for
        :param sp_name_qualifier: The unique identifier of the
            service provider or affiliation of providers for whom the
            identifier was generated.
        :param name_qualifier: The unique identifier of the identity
            provider that generated the identifier.
        :param nameid_format: The format of the name ID
        :param real_id: The identifier which is the key to this entity in the
            identity database
        :param binding: Which binding to use
        :return: The attributes returned
        """

        srvs = self.metadata.attribute_service(entityid, binding)
        if srvs == []:
            raise Exception("No attribute service support at entity")

        destination = destinations(srvs)[0]

        if real_id:
            response_args = {"real_id": real_id}
        else:
            response_args = {}

        if binding == BINDING_SOAP:
            return self.use_soap(destination, "attribute_query", consent=consent,
                                extensions=extensions, sign=sign,
                                subject_id=subject_id, attribute=attribute,
                                sp_name_qualifier=sp_name_qualifier,
                                name_qualifier=name_qualifier,
                                nameid_format=nameid_format,
                                response_args=response_args)
        elif binding == BINDING_HTTP_POST:
            return self.use_soap(destination, "attribute_query", consent=consent,
                                 extensions=extensions, sign=sign,
                                 subject_id=subject_id, attribute=attribute,
                                 sp_name_qualifier=sp_name_qualifier,
                                 name_qualifier=name_qualifier,
                                 nameid_format=nameid_format,
                                 response_args=response_args)
        else:
            raise Exception("Unsupported binding")