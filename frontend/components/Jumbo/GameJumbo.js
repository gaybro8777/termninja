import React from 'react';
import { Row,
         Col } from 'reactstrap';
import Jumbo from '.';


export const GameJumbo = ({ server_name, description }) => {

  return (
    <Jumbo>
      <Row className="align-items-center">
        <Col xs="12" sm="10" md="6">
          <h2 className="display-4">{ server_name }</h2>
          <small>{ description }</small>
        </Col>
      </Row>
  </Jumbo>
  )
}

